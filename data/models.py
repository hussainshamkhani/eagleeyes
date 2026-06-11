import uuid
from datetime import date, datetime
from enum import Enum
from typing import Dict, List, Optional
from pydantic import BaseModel, Field, ConfigDict, computed_field


class CustomerType(str, Enum):
    KUWAITI = "kuwaiti"
    RESIDENT = "resident"        # Has civil ID + residency article
    TOURIST = "tourist"          # Passport only, no residency
    CORPORATE = "corporate"      # Company / institution


class SenderProfile(BaseModel):
    """Represent the KYC record created at onboarding."""
    model_config = ConfigDict(use_enum_values=True)

    sender_id: str = Field(description="Civil ID number or passport number. Primary key.")
    full_name: str
    nationality: str = Field(description="ISO 3166-1 alpha-2 country code (e.g. 'IN', 'PH', 'EG', 'KW')")
    customer_type: CustomerType
    residency_article: Optional[str] = Field(default=None, description="Article number as string ('17', '18', '22', etc.). None for Kuwaitis and tourists.")
    date_of_birth: date = Field(description="Used to detect minors (under 18)")
    monthly_income_kd: Optional[float] = Field(default=None, description="Declared monthly income from KYC form. None if not declared.")
    phone: str
    address: Optional[str] = None
    is_pep: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    @computed_field
    @property
    def yearly_income_kd(self) -> Optional[float]:
        """Returns monthly_income_kd * 12 if available, else None"""
        if self.monthly_income_kd is not None:
            return self.monthly_income_kd * 12.0
        return None

    @computed_field
    @property
    def is_minor(self) -> bool:
        """True if age < 18 based on date_of_birth"""
        today = date.today()
        # Calculate age correctly considering leap years and current day/month
        age = today.year - self.date_of_birth.year - (
            (today.month, today.day) < (self.date_of_birth.month, self.date_of_birth.day)
        )
        return age < 18


class Transaction(BaseModel):
    """Core transaction record representing a remittance."""
    ref_no: str = Field(description="Unique transaction reference number. Primary key.")
    amount: float = Field(description="Amount in original currency")
    currency: str = Field(description="3-letter ISO currency code (e.g. 'USD', 'INR', 'PHP')")
    amount_kd: float = Field(description="Amount converted to Kuwaiti Dinar")
    bank: str = Field(description="Receiving bank name")
    date: datetime = Field(description="Transaction timestamp")
    acc_number: str = Field(description="Recipient account number")
    sender_id: str = Field(description="Foreign key to SenderProfile.sender_id")
    sender_name: str = Field(description="Denormalized for fast access")
    sender_nationality: str = Field(description="ISO country code, denormalized from SenderProfile")
    sender_tel: str
    branch: str = Field(description="Branch name where transaction was processed")
    recipient_name: str
    recipient_country: str = Field(description="ISO country code of recipient's country")
    recipient_is_company: bool = False
    recipient_company_name: Optional[str] = None
    transaction_purpose: str = Field(description="Free-text field for stated purpose")
    sender_is_corporate: bool = False
    sender_company_name: Optional[str] = None
    proof_of_wealth_provided: bool = False
    proof_of_relationship_provided: bool = False


class RuleViolation(BaseModel):
    """Represents a single rule triggered during evaluation."""
    rule_id: str = Field(description="e.g. 'SANCTIONED_COUNTRY'")
    rule_name: str = Field(description="Human-readable name")
    description: str = Field(description="What was detected")
    base_weight: float = Field(description="From RULE_WEIGHTS constant")
    contributing_factors: List[str] = Field(description="List of other active rules that amplify this one")


class RiskScore(BaseModel):
    """Output of the rule engine for a single transaction."""
    base_score: float = Field(description="Sum of base weights of triggered rules")
    behavior_multiplier: float = Field(default=1.0, description="Applied when velocity or recurrence detected (1.0–2.5)")
    recurrence_multiplier: float = Field(default=1.0, description="Applied for repeat offenders (1.0–2.0)")
    network_multiplier: float = Field(default=1.0, description="Applied for shared identifiers / coordinated senders (1.0–2.0)")
    final_score: float = Field(description="base_score * behavior_multiplier * recurrence_multiplier * network_multiplier, capped at 100")
    risk_level: str = Field(description="'LOW', 'MEDIUM', or 'HIGH' based on final_score vs thresholds")
    rules_triggered: List[RuleViolation]


class Alert(BaseModel):
    """Generated when a transaction is flagged."""
    alert_id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="UUID4, generated on creation")
    ref_no: str = Field(description="Foreign key to Transaction.ref_no")
    sender_id: str
    risk_score: RiskScore
    gemini_reasoning: str = Field(description="The Gemini agent's narrative explanation")
    gemini_confidence: float = Field(description="0.0–1.0")
    status: str = Field(description="'PENDING', 'REVIEWED_CLEARED', 'REVIEWED_ESCALATED', 'STR_FILED'")
    str_generated: bool = False
    str_content: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    reviewed_at: Optional[datetime] = None
    reviewed_by: Optional[str] = None
    reviewer_notes: Optional[str] = None
    arize_trace_id: Optional[str] = Field(default=None, description="Arize Phoenix trace ID for this evaluation")
    recommended_action: Optional[str] = None
    user_status: Optional[str] = None
    comment: Optional[str] = None
    updated_at: Optional[datetime] = None



class SelfImprovementReport(BaseModel):
    """Generated every 500 transactions by the self-improvement loop."""
    report_id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="UUID4")
    batch_number: int = Field(description="Which batch of 500 this covers")
    transactions_evaluated: int
    total_alerts_generated: int
    false_positive_estimate: float = Field(description="Percentage cleared by reviewers")
    false_negative_estimate: float = Field(description="Estimated misses (from Arize trace analysis)")
    rule_weight_adjustments: Dict[str, float] = Field(description="Rule ID -> new suggested weight")
    keyword_additions: List[str] = Field(description="New suspicious keywords identified")
    keyword_removals: List[str] = Field(description="Keywords that generated too many false positives")
    threshold_adjustments: Dict[str, float] = Field(description="Threshold name -> new value")
    gemini_analysis: str = Field(description="Gemini's narrative on what changed and why")
    applied: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)
    applied_at: Optional[datetime] = None


class STRReport(BaseModel):
    """Suspicious Transaction Report document structure."""
    model_config = ConfigDict(use_enum_values=True)

    str_id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="UUID4")
    alert_id: str
    ref_no: str
    report_date: datetime = Field(default_factory=datetime.utcnow)
    reporting_entity: str = "Al-Tawoos International Exchange Co."
    branch: str
    subject_name: str = Field(description="Sender full name")
    subject_id: str = Field(description="Civil ID or passport")
    subject_nationality: str
    subject_customer_type: CustomerType
    transaction_date: datetime
    transaction_amount_kd: float
    recipient_name: str
    recipient_country: str
    transaction_purpose: str
    suspicion_grounds: List[str] = Field(description="List of rule violations in plain English")
    risk_level: str
    risk_score: float
    narrative: str = Field(description="Full compliance narrative generated by Gemini")
    recommended_action: str = Field(description="e.g. 'File with CBK FIU', 'Internal hold', 'Enhanced monitoring'")
    generated_by: str = "EagleEyes AML Agent v1.0"
    str_content: Optional[str] = Field(default=None, description="The formatted plain-text STR report contents")
