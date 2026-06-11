"""
EagleEyes AML Domain Constants.
Defines all risk weights, thresholds, and domain-specific keywords for Kuwait AML/CFT regulations.
"""

# Risk Weights (Rule ID -> base weight score out of 100)
RULE_WEIGHTS = {
    "SANCTIONED_COUNTRY": 100,
    "STRUCTURING_MULTI_SENDER": 97,
    "SHARED_IDENTIFIER_NETWORK": 94,
    "REPEAT_FLAGS": 90,
    "INCOME_MISMATCH": 87,
    "TOURIST_NO_POW": 83,
    "ARTICLE_22_BREACH": 80,
    "NON_HOME_CORRIDOR": 72,
    "CORPORATE_PURPOSE_MISMATCH": 69,
    "INDIVIDUAL_TO_COMPANY": 65,
    "VAGUE_PURPOSE": 58,
    "MINOR_SENDER": 50,
}

# Risk Level Thresholds
RISK_THRESHOLDS = {
    "HIGH": 75,
    "MEDIUM": 50,
    "LOW": 0,
}

# CBK Thresholds and Window Settings
CBK_CASH_THRESHOLD_KD = 3000
TOURIST_POW_THRESHOLD_KD = 1000
ARTICLE_22_MONTHLY_LIMIT_KD = 150
ARTICLE_22_YEARLY_LIMIT_KD = 1000
REPEAT_FLAG_WINDOW_DAYS = 30
REPEAT_FLAG_COUNT_THRESHOLD = 3
STRUCTURING_WINDOW_SAME_DAY = "1D"
STRUCTURING_WINDOW_SAME_WEEK = "7D"
STRUCTURING_WINDOW_SAME_MONTH = "30D"
from core.config import settings
SELF_IMPROVE_BATCH_SIZE = settings.SELF_IMPROVE_AFTER_N_TRANSACTIONS

# Sanctioned / High-Risk Countries
SANCTIONED_COUNTRIES = ["IR", "IRL", "Iran", "IRN"]

# Valid Exception Keywords for Non-Home Corridor
VALID_CORRIDOR_EXCEPTION_KEYWORDS = [
    "education", "tuition", "university fees", "school fees", "student",
    "scholarship", "academic", "enrollment", "college", "institute",
    "medical", "treatment", "hospital", "surgery", "healthcare",
    "clinic", "medicine", "therapy", "dental", "pharmacy",
    "family support", "dependent", "spouse support", "child support",
    "parent support", "relative", "family member", "household",
    "property", "property purchase", "real estate", "land purchase",
    "mortgage", "down payment", "apartment",
    "investment", "business", "salary", "wages", "pension", "retirement",
]

PROPERTY_ALERT_KEYWORDS = [
    "property", "property purchase", "real estate", "land purchase",
    "mortgage", "down payment", "apartment",
]

# Vague Purpose Keywords
VAGUE_PURPOSE_KEYWORDS = [
    "general", "general payment", "other", "help", "personal",
    "miscellaneous", "misc", "unknown", "n/a", "none", "various",
    "gift", "donation", "assistance",
]

# Legitimate Corporate Purpose Keywords
LEGITIMATE_CORPORATE_PURPOSES = [
    "logistics", "transportation", "shipping", "freight", "cargo",
    "import", "export", "customs", "clearance", "trade", "supplier",
    "invoice", "procurement", "goods", "merchandise", "supply chain",
]

LEGITIMATE_CORPORATE_NAME_KEYWORDS = [
    "trading", "general trading", "contracting", "import", "export",
    "logistics", "shipping", "freight", "supply",
]

# Exempt Recipient Company Types
EXEMPT_RECIPIENT_TYPES = [
    "school", "university", "college", "institute", "academy",
    "hospital", "clinic", "medical center", "healthcare",
]

# Kuwait Residency Articles
RESIDENCY_ARTICLES = {
    "14": {"name": "Temporary Residency", "treat_as_tourist": True},
    "17": {"name": "Government Sector Employee", "treat_as_tourist": False},
    "18": {"name": "Private Sector Employee", "treat_as_tourist": False},
    "19": {"name": "Self-Employed / Business Owner", "treat_as_tourist": False},
    "20": {"name": "Domestic Worker", "treat_as_tourist": False, "income_check": True},
    "21": {"name": "Investor", "treat_as_tourist": False},
    "22": {"name": "Family Reunification (Non-Working)", "monthly_limit_kd": 150, "yearly_limit_kd": 1000},
    "23": {"name": "Student Residency", "treat_as_tourist": True},
    "24": {"name": "Self-Sponsored / Independent Income", "treat_as_tourist": False},
    "25": {"name": "Foreign Property Owner", "treat_as_tourist": False},
    "26": {"name": "Spouse of Kuwaiti", "treat_as_expat": True},
    "27": {"name": "Child of Kuwaiti Woman", "treat_as_expat": True},
    "28": {"name": "Widow/Divorced of Kuwaiti with Children", "treat_as_expat": True},
    "29": {"name": "Sponsored Extended Family", "treat_as_expat": True},
    "31": {"name": "Clergy / Religious Personnel", "treat_as_tourist": False},
}

REGULATION_REFERENCES = {
    "SANCTIONED_COUNTRY": "FATF Recommendation 7 — Targeted Financial Sanctions; CBK AML/CFT Circular 2/2014",
    "STRUCTURING_MULTI_SENDER": "FATF Recommendation 29 — Financial Intelligence Units; CBK Circular on Suspicious Transaction Reporting",
    "SHARED_IDENTIFIER_NETWORK": "FATF Recommendation 10 — Customer Due Diligence; CBK AML/CFT Guidelines Section 4",
    "REPEAT_FLAGS": "FATF Recommendation 20 — Reporting of Suspicious Transactions; CBK AML/CFT Guidelines Section 6",
    "INCOME_MISMATCH": "FATF Recommendation 10 — Customer Due Diligence (Source of Funds); CBK KYC Requirements Article 8",
    "TOURIST_NO_POW": "CBK Instructions for Exchange Companies — Article 12 (Proof of Wealth for Large Transfers)",
    "ARTICLE_22_BREACH": "CBK Exchange Company Regulations — Residency Article 22 Transfer Limits",
    "NON_HOME_CORRIDOR": "CBK Exchange Company Regulations — Corridor Restrictions for Non-Kuwaiti Residents",
    "CORPORATE_PURPOSE_MISMATCH": "FATF Recommendation 22 — DNFBPs: Customer Due Diligence; FATF Trade-Based Money Laundering Guidance",
    "INDIVIDUAL_TO_COMPANY": "FATF Recommendation 10 — Customer Due Diligence; CBK AML/CFT Guidelines Section 5",
    "VAGUE_PURPOSE": "CBK KYC Requirements — Transaction Purpose Documentation",
    "MINOR_SENDER": "CBK Exchange Company Regulations — Customer Eligibility Requirements",
}

