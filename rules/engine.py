import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

from data.models import Transaction, SenderProfile, Alert, RiskScore, RuleViolation, CustomerType
from core.constants import (
    RULE_WEIGHTS,
    RISK_THRESHOLDS,
    SANCTIONED_COUNTRIES,
    VALID_CORRIDOR_EXCEPTION_KEYWORDS,
    PROPERTY_ALERT_KEYWORDS,
    VAGUE_PURPOSE_KEYWORDS,
    LEGITIMATE_CORPORATE_PURPOSES,
    LEGITIMATE_CORPORATE_NAME_KEYWORDS,
    EXEMPT_RECIPIENT_TYPES,
    CBK_CASH_THRESHOLD_KD,
    TOURIST_POW_THRESHOLD_KD,
    ARTICLE_22_MONTHLY_LIMIT_KD,
    ARTICLE_22_YEARLY_LIMIT_KD
)
from opentelemetry import trace
from integrations.arize_client import tracer, ALL_RULE_IDS
from agent.self_improvement import get_active_weights, get_active_thresholds, get_active_keywords

logger = logging.getLogger("eagleeyes.rules")


class RuleEngine:
    """Evaluates a transaction and sender profile against 12 FATF/CBK-aligned rules."""

    def __init__(self, db):
        self.db = db  # MongoDB async motor database
        self.weights = RULE_WEIGHTS
        self.thresholds = RISK_THRESHOLDS

    @tracer.chain(name="rule_engine_evaluation")
    async def evaluate(
        self,
        transaction: Transaction,
        sender: SenderProfile,
        historical_transactions: List[Transaction],   # All prior txns for this sender
        all_transactions: List[Transaction],           # Full dataset (for network analysis)
        flag_history: List[Alert],                     # Prior alerts for this sender
    ) -> RiskScore:
        """Run all compliance and fraud rules on a single transaction and return a RiskScore."""
        logger.info(f"Evaluating transaction {transaction.ref_no} for sender {sender.sender_id}")
        
        # Load dynamic configurations
        self.weights = await get_active_weights(self.db)
        active_thresholds = await get_active_thresholds(self.db)
        self.thresholds = {
            "HIGH": active_thresholds.get("RISK_HIGH_THRESHOLD", active_thresholds.get("HIGH", RISK_THRESHOLDS["HIGH"])),
            "MEDIUM": active_thresholds.get("RISK_MEDIUM_THRESHOLD", active_thresholds.get("MEDIUM", RISK_THRESHOLDS["MEDIUM"])),
            "LOW": active_thresholds.get("LOW", RISK_THRESHOLDS["LOW"]),
        }
        
        active_kws = await get_active_keywords(self.db)
        vague_kws = active_kws.get("vague", VAGUE_PURPOSE_KEYWORDS)
        exception_kws = active_kws.get("valid_exceptions", VALID_CORRIDOR_EXCEPTION_KEYWORDS)
        
        self.article_22_limit = active_thresholds.get("ARTICLE_22_MONTHLY_LIMIT_KD", ARTICLE_22_MONTHLY_LIMIT_KD)

        triggered_rules: List[RuleViolation] = []
        
        # Senders prior transactions including current
        sender_txns = list(historical_transactions)
        if not any(t.ref_no == transaction.ref_no for t in sender_txns):
            sender_txns.append(transaction)

        # Full transactions list including current
        txns_to_check = list(all_transactions)
        if not any(t.ref_no == transaction.ref_no for t in txns_to_check):
            txns_to_check.append(transaction)

        # ----------------------------------------------------
        # RULE 1 — SANCTIONED_COUNTRY
        # ----------------------------------------------------
        rec_country = transaction.recipient_country.upper()
        sanctioned_upper = [c.upper() for c in SANCTIONED_COUNTRIES]
        if any(c in rec_country for c in sanctioned_upper) or any(rec_country in c for c in sanctioned_upper):
            violation = RuleViolation(
                rule_id="SANCTIONED_COUNTRY",
                rule_name="Sanctioned Country",
                description=f"Transaction recipient country '{transaction.recipient_country}' matches sanctioned country list.",
                base_weight=float(self.weights["SANCTIONED_COUNTRY"]),
                contributing_factors=[
                    f"Sender Nationality: {sender.nationality}",
                    f"Transaction Purpose: {transaction.transaction_purpose}",
                    f"Amount: {transaction.amount_kd} KD"
                ]
            )
            triggered_rules.append(violation)
            logger.info("Rule triggered: SANCTIONED_COUNTRY")
        else:
            logger.debug("Rule not triggered: SANCTIONED_COUNTRY")

        # ----------------------------------------------------
        # RULE 2 — STRUCTURING_MULTI_SENDER
        # ----------------------------------------------------
        # Senders sending to same acc_number
        rec_txns = [t for t in txns_to_check if t.acc_number == transaction.acc_number]
        
        day_triggered = False
        week_triggered = False
        month_triggered = False
        
        # Sub-level evaluation
        # Same Day
        day_txns = [t for t in rec_txns if abs((t.date - transaction.date).total_seconds()) <= 86400]
        if len(set(t.sender_id for t in day_txns)) >= 2:
            combined = sum(t.amount_kd for t in day_txns)
            if combined > CBK_CASH_THRESHOLD_KD and all(t.amount_kd < CBK_CASH_THRESHOLD_KD for t in day_txns):
                day_triggered = True

        # Same Week
        week_txns = [t for t in rec_txns if abs((t.date - transaction.date).total_seconds()) <= 7 * 86400]
        if len(set(t.sender_id for t in week_txns)) >= 2:
            combined = sum(t.amount_kd for t in week_txns)
            if combined > CBK_CASH_THRESHOLD_KD and all(t.amount_kd < CBK_CASH_THRESHOLD_KD for t in week_txns):
                week_triggered = True

        # Same 30 Days
        month_txns = [t for t in rec_txns if abs((t.date - transaction.date).total_seconds()) <= 30 * 86400]
        if len(set(t.sender_id for t in month_txns)) >= 2:
            combined = sum(t.amount_kd for t in month_txns)
            if combined > CBK_CASH_THRESHOLD_KD and all(t.amount_kd < CBK_CASH_THRESHOLD_KD for t in month_txns):
                month_triggered = True

        if day_triggered or week_triggered or month_triggered:
            window_str = "day" if day_triggered else ("week" if week_triggered else "30 days")
            violation = RuleViolation(
                rule_id="STRUCTURING_MULTI_SENDER",
                rule_name="Structuring Multi-Sender",
                description=f"Potential structuring network sending below limit to recipient {transaction.acc_number} within same {window_str}.",
                base_weight=float(self.weights["STRUCTURING_MULTI_SENDER"]),
                contributing_factors=[
                    f"Senders Involved: {list(set(t.sender_id for t in month_txns))}",
                    f"Time windows triggered - Day: {day_triggered}, Week: {week_triggered}, 30D: {month_triggered}"
                ]
            )
            triggered_rules.append(violation)
            logger.info("Rule triggered: STRUCTURING_MULTI_SENDER")
        else:
            logger.debug("Rule not triggered: STRUCTURING_MULTI_SENDER")

        # ----------------------------------------------------
        # RULE 3 — SHARED_IDENTIFIER_NETWORK
        # ----------------------------------------------------
        other_senders_same_rec = list(set(t.sender_id for t in rec_txns if t.sender_id != sender.sender_id))
        shared_network = False
        cf_network = []
        
        if other_senders_same_rec:
            # Query db for other senders profiles
            cursor = self.db.customers.find({"sender_id": {"$in": other_senders_same_rec}})
            other_profiles = await cursor.to_list(length=100)
            
            for op in other_profiles:
                phone_match = op.get("phone") == sender.phone
                addr_match = op.get("address") == sender.address if sender.address else False
                
                if phone_match or addr_match:
                    combined_sum = sum(t.amount_kd for t in rec_txns if t.sender_id in [sender.sender_id, op["sender_id"]])
                    if combined_sum > CBK_CASH_THRESHOLD_KD:
                        shared_network = True
                        match_type = "phone" if phone_match else "address"
                        cf_network.append(f"Shares {match_type} with sender {op['sender_id']} to same recipient {transaction.acc_number}")
                        break

        if shared_network:
            violation = RuleViolation(
                rule_id="SHARED_IDENTIFIER_NETWORK",
                rule_name="Shared Identifier Network",
                description=f"Multiple senders sharing physical identifiers transferring > {CBK_CASH_THRESHOLD_KD} KD to recipient {transaction.acc_number}.",
                base_weight=float(self.weights["SHARED_IDENTIFIER_NETWORK"]),
                contributing_factors=cf_network
            )
            triggered_rules.append(violation)
            logger.info("Rule triggered: SHARED_IDENTIFIER_NETWORK")
        else:
            logger.debug("Rule not triggered: SHARED_IDENTIFIER_NETWORK")

        # ----------------------------------------------------
        # RULE 4 — REPEAT_FLAGS
        # ----------------------------------------------------
        recent_flags = [f for f in flag_history if abs((transaction.date - f.created_at).total_seconds()) <= 30 * 86400]
        if len(recent_flags) >= 3:
            violation = RuleViolation(
                rule_id="REPEAT_FLAGS",
                rule_name="Repeat Flags",
                description=f"Sender has been flagged {len(recent_flags)} times in the last 30 days.",
                base_weight=float(self.weights["REPEAT_FLAGS"]),
                contributing_factors=[f"Flag Dates: {[f.created_at.isoformat() for f in recent_flags]}"]
            )
            triggered_rules.append(violation)
            logger.info("Rule triggered: REPEAT_FLAGS")
        else:
            logger.debug("Rule not triggered: REPEAT_FLAGS")

        # ----------------------------------------------------
        # RULE 5 — INCOME_MISMATCH
        # ----------------------------------------------------
        income_mismatch = False
        cf_income = []
        if sender.monthly_income_kd is not None and sender.monthly_income_kd > 0:
            if sender.customer_type in [CustomerType.RESIDENT, CustomerType.KUWAITI]:
                # Single transaction breach
                if transaction.amount_kd > sender.monthly_income_kd:
                    income_mismatch = True
                    cf_income.append(f"Single txn amount ({transaction.amount_kd} KD) exceeds monthly income ({sender.monthly_income_kd} KD)")
                
                # Monthly sum breach
                month_txns = [t for t in sender_txns if t.date.year == transaction.date.year and t.date.month == transaction.date.month]
                monthly_total = sum(t.amount_kd for t in month_txns)
                if monthly_total > sender.monthly_income_kd:
                    income_mismatch = True
                    cf_income.append(f"Monthly total ({monthly_total} KD) exceeds monthly income ({sender.monthly_income_kd} KD)")

                # Yearly sum breach
                year_txns = [t for t in sender_txns if t.date.year == transaction.date.year]
                yearly_total = sum(t.amount_kd for t in year_txns)
                yearly_income = sender.yearly_income_kd or (sender.monthly_income_kd * 12.0)
                if yearly_total > yearly_income:
                    income_mismatch = True
                    cf_income.append(f"Yearly total ({yearly_total} KD) exceeds yearly income ({yearly_income} KD)")

        if income_mismatch:
            violation = RuleViolation(
                rule_id="INCOME_MISMATCH",
                rule_name="Income Mismatch",
                description="Transaction amount or frequency is disproportionate to sender's declared income.",
                base_weight=float(self.weights["INCOME_MISMATCH"]),
                contributing_factors=cf_income
            )
            triggered_rules.append(violation)
            logger.info("Rule triggered: INCOME_MISMATCH")
        else:
            logger.debug("Rule not triggered: INCOME_MISMATCH")

        # ----------------------------------------------------
        # RULE 6 — TOURIST_NO_POW
        # ----------------------------------------------------
        is_tourist = sender.customer_type == CustomerType.TOURIST or sender.residency_article in ["14", "23"]
        if is_tourist and transaction.amount_kd >= TOURIST_POW_THRESHOLD_KD and not transaction.proof_of_wealth_provided:
            cf = ["No Proof of Wealth provided for high-value tourist remittance"]
            if not transaction.proof_of_relationship_provided:
                cf.append("No proof of relationship provided either")
            violation = RuleViolation(
                rule_id="TOURIST_NO_POW",
                rule_name="Tourist No Proof of Wealth",
                description=f"High-value tourist remittance ({transaction.amount_kd} KD) without standard Proof of Wealth documentation.",
                base_weight=float(self.weights["TOURIST_NO_POW"]),
                contributing_factors=cf
            )
            triggered_rules.append(violation)
            logger.info("Rule triggered: TOURIST_NO_POW")
        else:
            logger.debug("Rule not triggered: TOURIST_NO_POW")

        # ----------------------------------------------------
        # RULE 7 — ARTICLE_22_BREACH
        # ----------------------------------------------------
        if sender.residency_article == "22":
            month_txns = [t for t in sender_txns if t.date.year == transaction.date.year and t.date.month == transaction.date.month]
            monthly_total = sum(t.amount_kd for t in month_txns)
            
            year_txns = [t for t in sender_txns if t.date.year == transaction.date.year]
            yearly_total = sum(t.amount_kd for t in year_txns)
            
            m_breach = monthly_total > self.article_22_limit
            y_breach = yearly_total > ARTICLE_22_YEARLY_LIMIT_KD
            
            if m_breach or y_breach:
                violation = RuleViolation(
                    rule_id="ARTICLE_22_BREACH",
                    rule_name="Article 22 Breach",
                    description="Article 22 visa holder (dependent) exceeded legal remittance thresholds.",
                    base_weight=float(self.weights["ARTICLE_22_BREACH"]),
                    contributing_factors=[
                        f"Monthly Total: {monthly_total} KD (Limit: {self.article_22_limit} KD)",
                        f"Yearly Total: {yearly_total} KD (Limit: {ARTICLE_22_YEARLY_LIMIT_KD} KD)"
                    ]
                )
                triggered_rules.append(violation)
                logger.info("Rule triggered: ARTICLE_22_BREACH")
            else:
                logger.debug("Rule not triggered: ARTICLE_22_BREACH")
        else:
            logger.debug("Rule not triggered: ARTICLE_22_BREACH (Not Article 22)")

        # ----------------------------------------------------
        # RULE 8 — NON_HOME_CORRIDOR
        # ----------------------------------------------------
        if sender.customer_type != CustomerType.KUWAITI and sender.residency_article not in ["26", "27"]:
            if transaction.recipient_country != sender.nationality:
                if transaction.recipient_country not in SANCTIONED_COUNTRIES:
                    # Semantic keyword exception checks
                    has_exception = self._semantic_match(transaction.transaction_purpose, exception_kws)
                    has_property = self._semantic_match(transaction.transaction_purpose, PROPERTY_ALERT_KEYWORDS)
                    
                    if not has_exception or has_property:
                        cf = [f"Destination: {transaction.recipient_country} vs Home: {sender.nationality}"]
                        if has_property:
                            cf.append("Property-related transfer requires Proof of Wealth documentation — alert immediately")
                            
                        violation = RuleViolation(
                            rule_id="NON_HOME_CORRIDOR",
                            rule_name="Non-Home Corridor",
                            description="Expat transferring funds to non-home country without valid exceptions.",
                            base_weight=float(self.weights["NON_HOME_CORRIDOR"]),
                            contributing_factors=cf
                        )
                        triggered_rules.append(violation)
                        logger.info("Rule triggered: NON_HOME_CORRIDOR")
                    else:
                        logger.debug("Rule not triggered: NON_HOME_CORRIDOR (Exception keyword matched)")
                else:
                    logger.debug("Rule not triggered: NON_HOME_CORRIDOR (Sanctioned country handles it)")
            else:
                logger.debug("Rule not triggered: NON_HOME_CORRIDOR (Home country recipient)")
        else:
            logger.debug("Rule not triggered: NON_HOME_CORRIDOR (Kuwaiti or exempt visa)")

        # ----------------------------------------------------
        # RULE 9 — CORPORATE_PURPOSE_MISMATCH
        # ----------------------------------------------------
        if transaction.sender_is_corporate:
            name_has_legit = self._semantic_match(transaction.sender_company_name or "", LEGITIMATE_CORPORATE_NAME_KEYWORDS)
            purpose_has_legit = self._semantic_match(transaction.transaction_purpose, LEGITIMATE_CORPORATE_PURPOSES)
            
            personal_kws = ["family support", "personal", "household", "help"]
            purpose_is_personal = self._semantic_match(transaction.transaction_purpose, personal_kws)
            
            trigger = False
            if not name_has_legit and not purpose_has_legit:
                trigger = True
            elif name_has_legit and purpose_is_personal:
                trigger = True
                
            # Explicit exempt cases (name contains legit AND purpose contains legit -> exempt)
            if name_has_legit and purpose_has_legit:
                trigger = False
                
            if trigger:
                violation = RuleViolation(
                    rule_id="CORPORATE_PURPOSE_MISMATCH",
                    rule_name="Corporate Purpose Mismatch",
                    description="Corporate sender executing transaction with anomalous/personal purpose.",
                    base_weight=float(self.weights["CORPORATE_PURPOSE_MISMATCH"]),
                    contributing_factors=[
                        f"Company Name: {transaction.sender_company_name}",
                        f"Stated Purpose: {transaction.transaction_purpose}"
                    ]
                )
                triggered_rules.append(violation)
                logger.info("Rule triggered: CORPORATE_PURPOSE_MISMATCH")
            else:
                logger.debug("Rule not triggered: CORPORATE_PURPOSE_MISMATCH")
        else:
            logger.debug("Rule not triggered: CORPORATE_PURPOSE_MISMATCH (Not corporate sender)")

        # ----------------------------------------------------
        # RULE 10 — INDIVIDUAL_TO_COMPANY
        # ----------------------------------------------------
        if transaction.recipient_is_company and not transaction.sender_is_corporate:
            comp_name = transaction.recipient_company_name or ""
            is_exempt = self._semantic_match(comp_name, EXEMPT_RECIPIENT_TYPES)
            
            if not is_exempt:
                violation = RuleViolation(
                    rule_id="INDIVIDUAL_TO_COMPANY",
                    rule_name="Individual to Company Transfer",
                    description="Individual sender transferring funds to a corporate entity without education/medical exemptions.",
                    base_weight=float(self.weights["INDIVIDUAL_TO_COMPANY"]),
                    contributing_factors=[
                        f"Recipient Company: {transaction.recipient_company_name}",
                        f"Stated Purpose: {transaction.transaction_purpose}"
                    ]
                )
                triggered_rules.append(violation)
                logger.info("Rule triggered: INDIVIDUAL_TO_COMPANY")
            else:
                logger.debug("Rule not triggered: INDIVIDUAL_TO_COMPANY (Exempt recipient company)")
        else:
            logger.debug("Rule not triggered: INDIVIDUAL_TO_COMPANY")

        # ----------------------------------------------------
        # RULE 11 — VAGUE_PURPOSE
        # ----------------------------------------------------
        purp = transaction.transaction_purpose or ""
        vague_trigger = False
        
        if not purp or len(purp.strip()) < 5:
            vague_trigger = True
        elif self._semantic_match(purp, vague_kws):
            vague_trigger = True
            
        if vague_trigger:
            violation = RuleViolation(
                rule_id="VAGUE_PURPOSE",
                rule_name="Vague Transaction Purpose",
                description=f"Transaction purpose field '{purp}' is vague, incomplete, or contains alert keywords.",
                base_weight=float(self.weights["VAGUE_PURPOSE"]),
                contributing_factors=[f"Raw Purpose: {purp}"]
            )
            triggered_rules.append(violation)
            logger.info("Rule triggered: VAGUE_PURPOSE")
        else:
            logger.debug("Rule not triggered: VAGUE_PURPOSE")

        # ----------------------------------------------------
        # RULE 12 — MINOR_SENDER
        # ----------------------------------------------------
        if sender.is_minor:
            violation = RuleViolation(
                rule_id="MINOR_SENDER",
                rule_name="Minor Sender Alert",
                description="Underage customer (under 18 years old) initiated a remittance transaction.",
                base_weight=float(self.weights["MINOR_SENDER"]),
                contributing_factors=[f"Date of Birth: {sender.date_of_birth.isoformat()}"]
            )
            triggered_rules.append(violation)
            logger.info("Rule triggered: MINOR_SENDER")
        else:
            logger.debug("Rule not triggered: MINOR_SENDER")

        # ----------------------------------------------------
        # MULTIPLIERS & RISK CALCULATION
        # ----------------------------------------------------
        base_score = sum(rv.base_weight for rv in triggered_rules)
        
        # 1. Behavior Multiplier
        behavior_multiplier = self._calculate_behavior_multiplier(triggered_rules, sender_txns, transaction)
        
        # 2. Recurrence Multiplier
        recurrence_multiplier = self._calculate_recurrence_multiplier(flag_history)
        
        # 3. Network Multiplier
        network_multiplier = await self._calculate_network_multiplier(transaction, sender, txns_to_check)

        # Final Score Capped at 100
        final_score = min(100.0, base_score * behavior_multiplier * recurrence_multiplier * network_multiplier)
        
        # Assign risk level based on final score
        risk_level = "LOW"
        if final_score >= float(self.thresholds["HIGH"]):
            risk_level = "HIGH"
        elif final_score >= float(self.thresholds["MEDIUM"]):
            risk_level = "MEDIUM"

        # Auto-escalation: If REPEAT_FLAGS triggered, min risk level is MEDIUM
        is_repeat = any(rv.rule_id == "REPEAT_FLAGS" for rv in triggered_rules)
        if is_repeat and risk_level == "LOW":
            risk_level = "MEDIUM"
            logger.info("Risk level auto-escalated to MEDIUM due to REPEAT_FLAGS rule trigger.")

        # Set telemetry attributes on active span if tracing is active
        span = trace.get_current_span()
        if span and span.is_recording():
            triggered_ids = [r.rule_id for r in triggered_rules]
            span.set_attribute("eagleeyes.rules_checked", ",".join(ALL_RULE_IDS))
            span.set_attribute("eagleeyes.rules_triggered", ",".join(triggered_ids) if triggered_ids else "none")
            span.set_attribute("eagleeyes.rules_triggered_count", len(triggered_ids))
            span.set_attribute("eagleeyes.base_score", float(base_score))
            span.set_attribute("eagleeyes.behavior_multiplier", float(behavior_multiplier))
            span.set_attribute("eagleeyes.recurrence_multiplier", float(recurrence_multiplier))
            span.set_attribute("eagleeyes.network_multiplier", float(network_multiplier))
            span.set_attribute("eagleeyes.final_score", float(final_score))
            span.set_attribute("eagleeyes.risk_level", risk_level)

        return RiskScore(
            base_score=float(base_score),
            behavior_multiplier=float(behavior_multiplier),
            recurrence_multiplier=float(recurrence_multiplier),
            network_multiplier=float(network_multiplier),
            final_score=float(final_score),
            risk_level=risk_level,
            rules_triggered=triggered_rules
        )

    # ----------------------------------------------------
    # Helper Methods
    # ----------------------------------------------------
    async def _get_sender_transaction_history(self, sender_id: str, days: int) -> List[Transaction]:
        """Fetch all transactions for a sender in the last N days."""
        cutoff = datetime.utcnow() - timedelta(days=days)
        cursor = self.db.transactions.find({
            "sender_id": sender_id,
            "date": {"$gte": cutoff}
        })
        results = await cursor.to_list(length=1000)
        return [Transaction(**txn) for txn in results]

    async def _get_recipient_transaction_group(self, acc_number: str, window_days: int) -> List[Transaction]:
        """Fetch all transactions to a given account within a time window."""
        cutoff = datetime.utcnow() - timedelta(days=window_days)
        cursor = self.db.transactions.find({
            "acc_number": acc_number,
            "date": {"$gte": cutoff}
        })
        results = await cursor.to_list(length=1000)
        return [Transaction(**txn) for txn in results]

    async def _get_sender_flag_history(self, sender_id: str, days: int = 30) -> List[Alert]:
        """Fetch all prior alerts for a sender in the last N days."""
        cutoff = datetime.utcnow() - timedelta(days=days)
        cursor = self.db.alerts.find({
            "sender_id": sender_id,
            "created_at": {"$gte": cutoff}
        })
        results = await cursor.to_list(length=1000)
        return [Alert(**alert) for alert in results]

    def _calculate_behavior_multiplier(
        self, violations: List[RuleViolation], sender_txns: List[Transaction], transaction: Transaction
    ) -> float:
        """Aggregate behavior multiplier from all triggered rules."""
        behavior_multiplier = 1.0
        
        # +0.3 per velocity signal (high frequency same sender, same day/week)
        same_day_txns = [t for t in sender_txns if abs((t.date - transaction.date).total_seconds()) <= 86400]
        same_week_txns = [t for t in sender_txns if abs((t.date - transaction.date).total_seconds()) <= 7 * 86400]
        
        if len(same_day_txns) >= 3:
            behavior_multiplier += 0.3
            logger.debug("Velocity signal: Same day frequency >= 3. Added 0.3 to behavior multiplier.")
        if len(same_week_txns) >= 5:
            behavior_multiplier += 0.3
            logger.debug("Velocity signal: Same week frequency >= 5. Added 0.3 to behavior multiplier.")

        # Multiplier adjustments from rule sub-levels
        # Rule 2: Structuring windows
        has_structuring = any(rv.rule_id == "STRUCTURING_MULTI_SENDER" for rv in violations)
        if has_structuring:
            # We look at same day/week/30D triggers
            rec_txns = [t for t in sender_txns if t.acc_number == transaction.acc_number]
            day_t = [t for t in rec_txns if abs((t.date - transaction.date).total_seconds()) <= 86400]
            week_t = [t for t in rec_txns if abs((t.date - transaction.date).total_seconds()) <= 7 * 86400]
            
            # Select the tightest window
            if len(set(t.sender_id for t in day_t)) >= 2:
                behavior_multiplier += 0.5
            elif len(set(t.sender_id for t in week_t)) >= 2:
                behavior_multiplier += 0.3
            else:
                behavior_multiplier += 0.1

        # Rule 5: Income mismatch velocity sub-levels
        has_mismatch = any(rv.rule_id == "INCOME_MISMATCH" for rv in violations)
        if has_mismatch:
            # Recheck sender's income checks
            # Fetch mismatch from list of contributing factors or compute
            # For robustness, we check the violations details or add increments safely:
            # We can find the contributing factors to identify monthly total 2x/3x or annual breach
            mismatch_violation = next(rv for rv in violations if rv.rule_id == "INCOME_MISMATCH")
            cf_text = " ".join(mismatch_violation.contributing_factors).lower()
            
            if "3x" in cf_text or "3×" in cf_text:
                behavior_multiplier += 0.5
            elif "2x" in cf_text or "2×" in cf_text:
                behavior_multiplier += 0.2
                
            if "yearly" in cf_text or "annual" in cf_text:
                behavior_multiplier += 0.3

        # Rule 7: Article 22 visa dependent breach sub-levels
        has_art22 = any(rv.rule_id == "ARTICLE_22_BREACH" for rv in violations)
        if has_art22:
            art22_violation = next(rv for rv in violations if rv.rule_id == "ARTICLE_22_BREACH")
            desc_text = " ".join(art22_violation.contributing_factors).lower()
            
            # Sub-levels:
            # Monthly only: base weight (no multiplier increment)
            # Annual breach only: behavior_multiplier += 0.3
            # Both breached: behavior_multiplier += 0.5
            has_m = "monthly total" in desc_text or "limit: 150" in desc_text
            has_y = "yearly total" in desc_text or "limit: 1000" in desc_text
            # Check if breached actual amounts in description or recompute
            # The violation factors in eval log limits. Let's inspect the breach details:
            # We can read description text safely
            m_breached = False
            y_breached = False
            
            # Re-evaluate breach safely using totals
            month_txns = [t for t in sender_txns if t.date.year == transaction.date.year and t.date.month == transaction.date.month]
            m_total = sum(t.amount_kd for t in month_txns)
            year_txns = [t for t in sender_txns if t.date.year == transaction.date.year]
            y_total = sum(t.amount_kd for t in year_txns)
            
            article_22_limit = getattr(self, "article_22_limit", ARTICLE_22_MONTHLY_LIMIT_KD)
            if m_total > article_22_limit:
                m_breached = True
            if y_total > ARTICLE_22_YEARLY_LIMIT_KD:
                y_breached = True
                
            if m_breached and y_breached:
                behavior_multiplier += 0.5
            elif y_breached:
                behavior_multiplier += 0.3

        return behavior_multiplier

    def _calculate_recurrence_multiplier(self, flag_history: List[Alert]) -> float:
        """Calculate recurrence multiplier from flag history."""
        # 1.0 base; +0.5 if sender flagged 2× before; +1.0 if 3+ times in 30 days
        # "last 30 days" check
        # flag_history are prior alerts
        recent_flags = [f for f in flag_history if abs((datetime.utcnow() - f.created_at).total_seconds()) <= 30 * 86400]
        
        recurrence_multiplier = 1.0
        if len(recent_flags) >= 3:
            recurrence_multiplier += 1.0
        elif len(recent_flags) == 2:
            recurrence_multiplier += 0.5
            
        return recurrence_multiplier

    async def _calculate_network_multiplier(
        self, transaction: Transaction, sender: SenderProfile, all_transactions: List[Transaction]
    ) -> float:
        """Detect shared identifiers and calculate network multiplier."""
        network_multiplier = 1.0
        
        # Filter all transactions to the same recipient account
        rec_txns = [t for t in all_transactions if t.acc_number == transaction.acc_number and t.sender_id != sender.sender_id]
        
        if rec_txns:
            other_sender_ids = list(set(t.sender_id for t in rec_txns))
            
            # Retrieve profiles of other senders
            cursor = self.db.customers.find({"sender_id": {"$in": other_sender_ids}})
            other_profiles = await cursor.to_list(length=100)
            
            shared_phone_or_address = False
            same_nationality = False
            
            for op in other_profiles:
                phone_match = op.get("phone") == sender.phone
                addr_match = op.get("address") == sender.address if sender.address else False
                
                if phone_match or addr_match:
                    shared_phone_or_address = True
                    if op.get("nationality") == sender.nationality:
                        same_nationality = True
                        break
            
            if shared_phone_or_address:
                # +0.5 if shared phone/address with another sender to same recipient
                network_multiplier += 0.5
                if same_nationality:
                    # +0.5 more if same nationality
                    network_multiplier += 0.5
                    
        return network_multiplier

    def _semantic_match(self, text: str, keyword_list: List[str]) -> bool:
        """Case-insensitive substring match of text against a list of keywords."""
        if not text:
            return False
        text_lower = text.lower()
        return any(kw.lower() in text_lower for kw in keyword_list)
