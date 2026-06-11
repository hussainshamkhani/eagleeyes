import random
from datetime import datetime
from typing import Dict, List, Optional

from data.models import Transaction, SenderProfile, Alert, STRReport, CustomerType, RiskScore
from core.constants import RESIDENCY_ARTICLES

# --- Branch Codes ---
BRANCH_CODES = {
    "Kuwait City Branch": "KWC",
    "Salmiya Branch": "SAL",
    "Hawalli Branch": "HAW",
    "Farwaniya Branch": "FAR",
    "Jahra Branch": "JAH",
    "Ahmadi Branch": "AHM",
}

# --- Country Names ---
COUNTRY_NAMES = {
    "IN": "India", "PH": "Philippines", "EG": "Egypt",
    "PK": "Pakistan", "BD": "Bangladesh", "NP": "Nepal",
    "LK": "Sri Lanka", "SY": "Syria", "JO": "Jordan",
    "KW": "Kuwait", "IR": "Iran (Islamic Republic)",
    "GB": "United Kingdom", "US": "United States",
    "AE": "United Arab Emirates", "SA": "Saudi Arabia",
    "QA": "Qatar", "OM": "Oman", "BH": "Bahrain",
    "YE": "Yemen", "IQ": "Iraq", "LB": "Lebanon",
    "RO": "Romania", "PL": "Poland", "UA": "Ukraine",
    "CZ": "Czech Republic",
}

# --- Customer Type Mapping ---
CUSTOMER_TYPE_MAPPING = {
    "kuwaiti": "Kuwaiti",
    "resident": "Resident",
    "tourist": "Tourist",
    "corporate": "Corporate"
}

# --- Separator ---
SEPARATOR = "══════════════════════════════════════════════════════════════"


def generate_str_id(branch: str, date: datetime) -> str:
    """
    Format: STR-{BRANCH_CODE}-{YYYYMMDD}-{4_DIGIT_SEQUENCE}
    Example: STR-KWC-20250517-0042
    """
    if not branch:
        branch_code = "UNK"
    elif branch in BRANCH_CODES:
        branch_code = BRANCH_CODES[branch]
    elif branch.upper() in BRANCH_CODES.values():
        branch_code = branch.upper()
    else:
        # Partial match lookup
        branch_code = "UNK"
        for b_name, b_code in BRANCH_CODES.items():
            if branch.lower() in b_name.lower():
                branch_code = b_code
                break
        if branch_code == "UNK":
            # Fallback based on abbreviation
            branch_code = "".join([c for c in branch if c.isupper()])[:3]
            if len(branch_code) < 3:
                branch_code = "KWC" # ultimate default
                
    date_str = date.strftime("%Y%m%d")
    seq = random.randint(1, 9999)
    return f"STR-{branch_code}-{date_str}-{seq:04d}"


class STRGenerator:
    """Generates official Suspicious Transaction Reports compliant with Kuwait FIU and CBK standard conventions."""

    def __init__(self):
        pass

    def generate(
        self,
        alert: Alert,
        transaction: Transaction,
        sender: SenderProfile,
        historical_context: dict,  # From MongoDB: 30d totals, annual totals, prior STR count
    ) -> STRReport:
        """
        Generate a complete STR report from an alert.
        Returns an STRReport model instance with str_content populated.
        """
        # Determine the recommended action first (e.g. FILE_STR, HOLD, MONITOR, CLEAR)
        rec_action = self._determine_recommended_disposition(alert)
        
        # Build human-friendly recommended action description for the STRReport field
        rec_action_map = {
            "FILE_STR": "File with CBK FIU",
            "HOLD": "Internal hold pending EDD",
            "MONITOR": "Enhanced monitoring (90 days)",
            "CLEAR": "Clear alert - no action"
        }
        recommended_action_desc = rec_action_map.get(rec_action, "Manual review required")
        
        # Formulate suspicion grounds
        suspicion_grounds = []
        if alert.risk_score and alert.risk_score.rules_triggered:
            for r in alert.risk_score.rules_triggered:
                suspicion_grounds.append(r.rule_name)
        if not suspicion_grounds:
            suspicion_grounds = ["Suspicious pattern detected"]
            
        report_date = datetime.utcnow()
        str_id = generate_str_id(transaction.branch, report_date)
        
        # Build the structured STR plain text document content
        str_content = self._build_str_text(alert, transaction, sender, historical_context)
        
        # Instantiate STRReport
        report = STRReport(
            str_id=str_id,
            alert_id=alert.alert_id,
            ref_no=transaction.ref_no,
            report_date=report_date,
            reporting_entity="Al-Tawoos International Exchange Co.",
            branch=transaction.branch,
            subject_name=sender.full_name,
            subject_id=sender.sender_id,
            subject_nationality=self._get_country_name(sender.nationality),
            subject_customer_type=sender.customer_type,
            transaction_date=transaction.date,
            transaction_amount_kd=transaction.amount_kd,
            recipient_name=transaction.recipient_name,
            recipient_country=self._get_country_name(transaction.recipient_country),
            transaction_purpose=transaction.transaction_purpose,
            suspicion_grounds=suspicion_grounds,
            risk_level=alert.risk_score.risk_level if alert.risk_score else "LOW",
            risk_score=alert.risk_score.final_score if alert.risk_score else 0.0,
            narrative=alert.gemini_reasoning,
            recommended_action=recommended_action_desc,
            generated_by="EagleEyes AML Agent v1.0",
            str_content=str_content
        )
        
        return report

    def _build_str_text(
        self,
        alert: Alert,
        transaction: Transaction,
        sender: SenderProfile,
        historical_context: dict,
    ) -> str:
        """
        Build the full plain-text STR document using the template above.
        Returns a formatted string ready for display or PDF export.
        """
        report_date = datetime.utcnow()
        str_id = generate_str_id(transaction.branch, report_date)
        
        # Formatting Helpers
        def format_amount(amount: Optional[float]) -> str:
            if amount is None:
                return "Not declared"
            return f"{amount:,.3f} KD"
            
        def format_date(dt: Optional[datetime]) -> str:
            if not dt:
                return "N/A"
            return dt.strftime("%d/%m/%Y %H:%M UTC")

        def format_date_only(d) -> str:
            if not d:
                return "N/A"
            return d.strftime("%d/%m/%Y")

        # Determine ID Type and residency article info
        if sender.customer_type == CustomerType.TOURIST or (sender.sender_id and sender.sender_id[0].isalpha()):
            id_type = "Passport"
        else:
            id_type = "Civil ID"
            
        residency_info = "N/A"
        if sender.residency_article and sender.residency_article != "N/A":
            art_info = RESIDENCY_ARTICLES.get(str(sender.residency_article))
            if art_info:
                residency_info = f"Article {sender.residency_article} - {art_info['name']}"
            else:
                residency_info = f"Article {sender.residency_article}"
                
        # Resolve customer type capitalized label
        cust_type_label = CUSTOMER_TYPE_MAPPING.get(str(sender.customer_type).lower(), str(sender.customer_type).capitalize())
        
        # Spacing and alignment prefix length
        align = 26
        
        # Historical context mapping
        hist_txns_30d = historical_context.get("txn_count_30d") or historical_context.get("total_transactions_30d") or historical_context.get("count_30d") or 0
        hist_amount_30d = historical_context.get("amount_kd_30d") or historical_context.get("total_amount_kd_30d") or historical_context.get("total_amount_sent_30d") or 0.0
        hist_amount_month = historical_context.get("amount_kd_this_month") or historical_context.get("total_amount_kd_this_month") or historical_context.get("total_amount_sent_this_month") or 0.0
        hist_amount_year = historical_context.get("amount_kd_this_year") or historical_context.get("total_amount_kd_this_year") or historical_context.get("total_amount_sent_this_year") or historical_context.get("annual_total_kd") or 0.0
        hist_alerts_30d = historical_context.get("prior_alerts_count") or historical_context.get("prior_alerts_30d") or historical_context.get("prior_alerts") or 0
        hist_strs_filed = historical_context.get("prior_strs_count") or historical_context.get("prior_strs_filed") or historical_context.get("prior_strs") or 0
        
        # Build Section 7 list
        additional_info_items = self._get_additional_info_required(alert)
        bulleted_additional_info = "\n".join([f"  • {item}" for item in additional_info_items])
        
        # Build Section 8 recommended action
        rec_action = self._determine_recommended_disposition(alert)
        recommended_action_block = self._format_recommended_action_section(rec_action)
        
        # Build Grounds for Suspicion Section 4
        grounds_block = self._format_rules_section(alert.risk_score) if alert.risk_score else "  No risk assessment available."

        # Template construction
        lines = [
            "SUSPICIOUS TRANSACTION REPORT (STR)",
            "Issued under: CBK AML/CFT Law No. 106 of 2013 and its Executive Regulations",
            "",
            SEPARATOR,
            "SECTION 1 — REPORTING ENTITY INFORMATION",
            SEPARATOR,
            f"{'Reporting Institution:':<{align}}Al-Tawoos International Exchange Co.",
            f"{'License Number:':<{align}}EC-106-2013",
            f"{'Branch:':<{align}}{transaction.branch}",
            f"{'Report Date:':<{align}}{format_date(report_date)}",
            f"{'Report Reference:':<{align}}{str_id}",
            f"{'Compliance Officer:':<{align}}EagleEyes AML Agent v1.0",
            f"{'Contact:':<{align}}compliance@eagleeyes.internal",
            "",
            SEPARATOR,
            "SECTION 2 — SUBJECT OF REPORT (SENDER)",
            SEPARATOR,
            f"{'Full Name:':<{align}}{sender.full_name}",
            f"{'ID Type:':<{align}}{id_type}",
            f"{'ID Number:':<{align}}{sender.sender_id}",
            f"{'Nationality:':<{align}}{self._get_country_name(sender.nationality)}",
            f"{'Customer Type:':<{align}}{cust_type_label}",
            f"{'Residency Article:':<{align}}{residency_info}",
            f"{'Date of Birth:':<{align}}{format_date_only(sender.date_of_birth)}",
            f"{'Phone Number:':<{align}}{sender.phone}",
            f"{'Address:':<{align}}{sender.address or 'Not on file'}",
            f"{'Monthly Declared Income:':<{align}}{format_amount(sender.monthly_income_kd)}",
            f"{'PEP Status:':<{align}}{'Yes' if sender.is_pep else 'No'}",
            "",
            SEPARATOR,
            "SECTION 3 — TRANSACTION DETAILS",
            SEPARATOR,
            f"{'Transaction Reference:':<{align}}{transaction.ref_no}",
            f"{'Transaction Date:':<{align}}{format_date(transaction.date)}",
            f"{'Amount (Original):':<{align}}{transaction.amount:,.2f} {transaction.currency.upper()}",
            f"{'Amount (KD Equivalent):':<{align}}{format_amount(transaction.amount_kd)}",
            f"{'Receiving Bank:':<{align}}{transaction.bank}",
            f"{'Recipient Name:':<{align}}{transaction.recipient_name}",
            f"{'Recipient Country:':<{align}}{self._get_country_name(transaction.recipient_country)}",
            f"{'Recipient Account:':<{align}}{transaction.acc_number}",
            f"{'Recipient Type:':<{align}}{'Company' if transaction.recipient_is_company else 'Individual'}",
            f"{'Transaction Purpose:':<{align}}{transaction.transaction_purpose}",
            f"{'Proof of Wealth:':<{align}}{'Provided' if transaction.proof_of_wealth_provided else 'Not provided'}",
            f"{'Proof of Relationship:':<{align}}{'Provided' if transaction.proof_of_relationship_provided else 'Not provided'}",
            f"{'Sender is Corporate:':<{align}}{'Yes' if transaction.sender_is_corporate else 'No'}",
            "",
            SEPARATOR,
            "SECTION 4 — GROUNDS FOR SUSPICION",
            SEPARATOR,
            grounds_block,
            "",
            SEPARATOR,
            "SECTION 5 — HISTORICAL CONTEXT",
            SEPARATOR,
            f"{'Total Transactions (30 days):':<34}{hist_txns_30d}",
            f"{'Total Amount Sent (30 days):':<34}{format_amount(hist_amount_30d)}",
            f"{'Total Amount Sent (This Month):':<34}{format_amount(hist_amount_month)}",
            f"{'Total Amount Sent (This Year):':<34}{format_amount(hist_amount_year)}",
            f"{'Prior Alerts (30 days):':<34}{hist_alerts_30d}",
            f"{'Prior STRs Filed:':<34}{hist_strs_filed}",
            "",
            SEPARATOR,
            "SECTION 6 — COMPLIANCE NARRATIVE",
            SEPARATOR,
            alert.gemini_reasoning.strip() if alert.gemini_reasoning else "No narrative narrative provided.",
            "",
            SEPARATOR,
            "SECTION 7 — ADDITIONAL INFORMATION REQUIRED",
            SEPARATOR,
            bulleted_additional_info,
            "",
            SEPARATOR,
            "SECTION 8 — RECOMMENDED ACTION",
            SEPARATOR,
            recommended_action_block,
            "",
            SEPARATOR,
            "SECTION 9 — CERTIFICATION",
            SEPARATOR,
            f"This report was generated automatically by EagleEyes AML Agent v1.0",
            f"on {format_date(report_date)}. All data is sourced from transaction records and KYC",
            f"profiles on file. This report is subject to review and approval by",
            f"a licensed compliance officer before submission to any authority.",
            "",
            f"Generated by: EagleEyes — Self-Improving AML Detection Agent",
            f"Powered by: Google Gemini + Arize Phoenix + MongoDB",
            f"CBK License Reference: EC-106-2013",
            SEPARATOR,
            "END OF REPORT",
            SEPARATOR
        ]
        
        return "\n".join(lines)

    def _format_rules_section(self, risk_score: RiskScore) -> str:
        """Format Section 4 — Grounds for Suspicion."""
        from core.constants import REGULATION_REFERENCES
        
        lines = [
            f"Risk Score:               {risk_score.final_score:.1f} / 100",
            f"Risk Level:               {risk_score.risk_level}",
            "",
            "Rules Triggered:"
        ]
        
        if risk_score.rules_triggered:
            for r in risk_score.rules_triggered:
                reg_ref = REGULATION_REFERENCES.get(r.rule_id, "CBK AML/CFT General Guidelines")
                lines.append(f"  ► {r.rule_name} (Severity: {int(r.base_weight)}/100)")
                lines.append(f"    Reason: {r.description}")
                lines.append(f"    Regulation: {reg_ref}")
        else:
            lines.append("  None")
            
        lines.extend([
            "",
            "Multipliers Applied:",
            f"  Behavior Multiplier:    {risk_score.behavior_multiplier:.2f}x",
            f"  Recurrence Multiplier:  {risk_score.recurrence_multiplier:.2f}x",
            f"  Network Multiplier:     {risk_score.network_multiplier:.2f}x"
        ])
        
        return "\n".join(lines)

    def _format_recommended_action_section(self, recommended_action: str) -> str:
        """Format Section 8 — Recommended Action with appropriate disposition text."""
        lines = [
            f"Agent Recommendation:     {recommended_action}",
            "Recommended Disposition:  "
        ]
        
        if recommended_action == "FILE_STR":
            lines.append("    → Submit to CBK Financial Intelligence Unit (FIU)")
            lines.append("    → Retain all records for minimum 5 years per CBK Circular 2/2014")
            lines.append("    → Do not tip off customer (tipping-off prohibition under AML Law Art. 22)")
            lines.append("    → Place transaction on internal watchlist")
        elif recommended_action == "HOLD":
            lines.append("    → Freeze transaction pending enhanced due diligence")
            lines.append("    → Request proof of source of funds within 3 business days")
            lines.append("    → Escalate to senior compliance officer")
        elif recommended_action == "MONITOR":
            lines.append("    → Allow transaction to proceed")
            lines.append("    → Flag sender for enhanced monitoring for 90 days")
            lines.append("    → Review all future transactions from this sender manually")
        elif recommended_action == "CLEAR":
            lines.append("    → No further action required")
            lines.append("    → Document clearance reason in alert record")
        else:
            lines.append("    → Manual review and disposition by compliance officer")
            
        return "\n".join(lines)

    def _determine_recommended_disposition(self, alert: Alert) -> str:
        """
        Map recommended_action to a formal disposition statement.
        Based on: agent recommendation + risk level + rules triggered.
        """
        rec = "HOLD"  # Default fallback
        
        # Check if status tells us the action directly
        if alert.status == "STR_FILED":
            return "FILE_STR"
        elif alert.status == "REVIEWED_CLEARED":
            return "CLEAR"
            
        # Parse from agent's narrative reasoning
        reasoning_upper = alert.gemini_reasoning.upper() if alert.gemini_reasoning else ""
        if "FILE_STR" in reasoning_upper or "FILE STR" in reasoning_upper or "SUBMIT STR" in reasoning_upper:
            rec = "FILE_STR"
        elif "HOLD" in reasoning_upper or "FREEZE" in reasoning_upper:
            rec = "HOLD"
        elif "MONITOR" in reasoning_upper or "ENHANCED MONITORING" in reasoning_upper:
            rec = "MONITOR"
        elif "CLEAR" in reasoning_upper or "CLEARED" in reasoning_upper:
            rec = "CLEAR"
        else:
            # Fallback based on risk level and triggers
            risk_level = alert.risk_score.risk_level if alert.risk_score else "LOW"
            if risk_level == "HIGH":
                rec = "FILE_STR"
            elif risk_level == "MEDIUM":
                triggered_rule_ids = {r.rule_id for r in alert.risk_score.rules_triggered} if alert.risk_score else set()
                if "STRUCTURING_MULTI_SENDER" in triggered_rule_ids or "SHARED_IDENTIFIER_NETWORK" in triggered_rule_ids or "SANCTIONED_COUNTRY" in triggered_rule_ids:
                    rec = "FILE_STR"
                elif "INCOME_MISMATCH" in triggered_rule_ids or "TOURIST_NO_POW" in triggered_rule_ids:
                    rec = "HOLD"
                else:
                    rec = "MONITOR"
            else:
                rec = "CLEAR"
                
        return rec

    def _get_additional_info_required(self, alert: Alert) -> List[str]:
        """Generate/retrieve a bulleted list of items for enhanced customer due diligence."""
        items = getattr(alert, "additional_info_required", None)
        if not items:
            items = alert.__dict__.get("additional_info_required")
            
        if not items:
            items = []
            rules = {r.rule_id for r in alert.risk_score.rules_triggered} if alert.risk_score else set()
            if "INCOME_MISMATCH" in rules:
                items.extend([
                    "Salary certificate or letter from employer confirming monthly salary",
                    "Bank statements for the last 3 months showing salary deposits",
                    "Explanation/documentation of any additional source of income"
                ])
            if "TOURIST_NO_POW" in rules:
                items.extend([
                    "Copy of visa/passport showing tourist status and entry date",
                    "Official Proof of Wealth (e.g., bank statement, customs cash declaration, credit card details)",
                    "Documented purpose of travel and source of funds for this remittance"
                ])
            if "ARTICLE_22_BREACH" in rules:
                items.extend([
                    "Sponsor's civil ID copy and contact details",
                    "Sponsor's salary certificate/income proof",
                    "Written explanation for the remittance exceeding the Article 22 limit"
                ])
            if "INDIVIDUAL_TO_COMPANY" in rules:
                items.extend([
                    "Official invoice or contract for the service/goods being paid for",
                    "Commercial registration copy of the receiving company",
                    "Proof of relationship with the corporate beneficiary"
                ])
            if "NON_HOME_CORRIDOR" in rules:
                items.extend([
                    "Proof of legitimate corridor exception (e.g., school tuition invoice, medical treatment bills, real estate contract)",
                    "Proof of relationship to the beneficiary (if personal corridor exception)"
                ])
            if "SANCTIONED_COUNTRY" in rules:
                items.extend([
                    "Enhanced due diligence (EDD) questionnaire completed by the sender",
                    "Full beneficiary background check (physical address, official registration, relationship to sender)",
                    "Verifiable documentation on final use of funds"
                ])
            if not items:
                items = [
                    "Copy of Civil ID / Passport of the sender for verification",
                    "Stated source of funds declaration form",
                    "Detailed relationship explanation between sender and beneficiary"
                ]
        return items

    def _get_country_name(self, code: str) -> str:
        """Resolve ISO-2 country code to full name, case-insensitively."""
        if not code:
            return "Unknown"
        return COUNTRY_NAMES.get(code.upper(), code)
