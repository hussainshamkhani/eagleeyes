import os
import sys
import json
import random
import uuid
import argparse
from datetime import date, datetime, timedelta
from typing import List, Dict, Any

from faker import Faker
from rich.console import Console
from rich.table import Table

from data.models import CustomerType, SenderProfile, Transaction
from core.constants import RULE_WEIGHTS

# Realistic Kuwait Remittance Demographics
SENDER_NATIONALITY_DISTRIBUTION = {
    "IN": 0.28,   # Indian — largest expat community
    "PH": 0.12,   # Filipino
    "EG": 0.10,   # Egyptian
    "PK": 0.09,   # Pakistani
    "BD": 0.08,   # Bangladeshi
    "NP": 0.06,   # Nepali
    "LK": 0.05,   # Sri Lankan
    "SY": 0.04,   # Syrian
    "JO": 0.04,   # Jordanian
    "KW": 0.07,   # Kuwaiti national
    "IR": 0.03,   # Iranian (high-risk — will trigger flags)
    "OTHER": 0.04 # Mix of other nationalities
}

ARTICLE_DISTRIBUTION = {
    "17": 0.10,
    "18": 0.40,   # Most common — private sector
    "19": 0.08,
    "20": 0.20,   # Domestic workers — large population
    "21": 0.02,
    "22": 0.06,
    "23": 0.02,
    "24": 0.03,
    "25": 0.01,
    "26": 0.03,
    "27": 0.02,
    "28": 0.01,
    "29": 0.01,
    "31": 0.01,
}

INCOME_RANGES_BY_ARTICLE = {
    "17": (300, 900),    # Government employee
    "18": (80, 500),     # Private sector (wide range)
    "19": (400, 2000),   # Self-employed
    "20": (50, 100),     # Domestic workers
    "21": (1000, 5000),  # Investors
    "22": (0, 0),        # Non-working dependants — no income
    "23": (0, 150),      # Students — minimal or none
    "24": (500, 3000),   # Independent income
    "25": (300, 2000),   # Property owners
    "26": (100, 400),    # Spouse of Kuwaiti
    "27": (0, 200),      # Child of Kuwaiti
    "28": (100, 300),    # Widow/Divorced
    "29": (0, 100),      # Sponsored family
    "31": (150, 400),    # Clergy
}

HOME_COUNTRY_MAP = {
    "IN": "IN", "PH": "PH", "EG": "EG", "PK": "PK",
    "BD": "BD", "NP": "NP", "LK": "LK", "SY": "SY",
    "JO": "JO", "KW": "ANY", "IR": "IR",
}

# Branch list
BRANCHES = [
    "Salmiya Branch", "Kuwait City Branch", "Hawalli Branch",
    "Farwaniya Branch", "Jahra Branch", "Ahmadi Branch"
]

# Bank list
BANKS = [
    "Western Union", "MoneyGram", "National Bank of Kuwait",
    "Gulf Bank", "Al Rajhi Bank", "Habib Bank", "Bank Muscat"
]

# Currencies mapping
CURRENCY_MAP = {
    "IN": "INR", "PH": "PHP", "EG": "EGP", "PK": "PKR",
    "BD": "BDT", "NP": "NPR", "LK": "LKR", "SY": "SYP",
    "JO": "JOD", "KW": "KWD", "IR": "IRR", "OTHER": "USD"
}

# Exponent conversion rate (1 KWD to other currencies, illustrative)
FX_RATES = {
    "INR": 272.50,
    "PHP": 185.00,
    "EGP": 150.00,
    "PKR": 910.00,
    "BDT": 360.00,
    "NPR": 435.00,
    "LKR": 980.00,
    "SYP": 4200.00,
    "JOD": 2.30,
    "KWD": 1.00,
    "IRR": 138000.00,
    "USD": 3.25
}


class DataGenerator:
    def __init__(self, seed: int = 42, n_customers: int = 500, n_transactions: int = 5000):
        self.seed = seed
        self.n_customers = n_customers
        self.n_transactions = n_transactions
        self.fake = Faker(locale="en_US")
        
        # Set seeds for reproducibility
        random.seed(seed)
        Faker.seed(seed)

        self.customers: List[Dict[str, Any]] = []
        self.transactions: List[Dict[str, Any]] = []
        self.ground_truth: Dict[str, List[str]] = {}
        
        # Track counts of patterns
        self.pattern_counts = {
            "CLEAN": 0,
            "SANCTIONED_COUNTRY": 0,
            "STRUCTURING_MULTI_SENDER": 0,
            "SHARED_IDENTIFIER_NETWORK": 0,
            "INCOME_MISMATCH": 0,
            "NON_HOME_CORRIDOR": 0,
            "ARTICLE_22_BREACH": 0,
            "CORPORATE_PURPOSE_MISMATCH": 0,
            "INDIVIDUAL_TO_COMPANY": 0,
            "MINOR_SENDER": 0,
            "REPEAT_FLAGS": 0,
            "TOURIST_NO_POW": 0,
            "VAGUE_PURPOSE": 0
        }

    def generate_customers(self) -> List[Dict[str, Any]]:
        """Generate SenderProfile records as dicts ready for MongoDB insert."""
        customers = []
        
        # 1. Generate structuring groups (3-5 senders sharing phone or address)
        # We need ~3% structuring transactions (150). Let's build 8 groups of 4 residents
        structuring_groups = []
        for g_idx in range(8):
            shared_phone = f"+965 {random.randint(5, 9)}{random.randint(1000000, 9999999)}"
            shared_address = f"Block {random.randint(1, 12)}, Street {random.randint(1, 90)}, Shared Building {g_idx + 1}, Hawalli, Kuwait"
            group_members = []
            for _ in range(4):
                sender_id = f"2{random.randint(80, 99)}{random.randint(10, 12)}{random.randint(10, 28)}{random.randint(10000, 99999)}"
                nat = random.choice(["IN", "PH", "EG", "PK"])
                dob = date(random.randint(1975, 2000), random.randint(1, 12), random.randint(1, 28))
                
                profile = SenderProfile(
                    sender_id=sender_id,
                    full_name=self.fake.name(),
                    nationality=nat,
                    customer_type=CustomerType.RESIDENT,
                    residency_article="18",
                    date_of_birth=dob,
                    monthly_income_kd=float(random.randint(250, 450)),
                    phone=shared_phone,
                    address=shared_address,
                    is_pep=False
                )
                group_members.append(profile.model_dump())
            structuring_groups.extend(group_members)
        customers.extend(structuring_groups)

        # 2. Generate tourists (Passport as ID)
        tourists = []
        for _ in range(50):
            sender_id = f"{random.choice(['A', 'B', 'E', 'Z'])}{random.randint(10000000, 99999999)}"
            nat = random.choice(["IN", "PH", "EG", "OTHER"])
            dob = date(random.randint(1970, 2002), random.randint(1, 12), random.randint(1, 28))
            
            profile = SenderProfile(
                sender_id=sender_id,
                full_name=self.fake.name(),
                nationality=nat,
                customer_type=CustomerType.TOURIST,
                date_of_birth=dob,
                monthly_income_kd=None,
                phone=f"+965 {random.choice([5, 6, 9])}{random.randint(1000000, 9999999)}",
                address=None,
                is_pep=False
            )
            tourists.append(profile.model_dump())
        customers.extend(tourists)

        # 3. Generate minors (age 14-17)
        minors = []
        for _ in range(15):
            sender_id = f"3100523{random.randint(10, 28)}{random.randint(10000, 99999)}"
            nat = random.choice(["IN", "PH", "EG", "KW"])
            # Born between 2008 and 2012
            dob = date(random.randint(2008, 2011), random.randint(1, 12), random.randint(1, 28))
            ctype = CustomerType.KUWAITI if nat == "KW" else CustomerType.RESIDENT
            res_art = None if nat == "KW" else "22"
            
            profile = SenderProfile(
                sender_id=sender_id,
                full_name=self.fake.name(),
                nationality=nat,
                customer_type=ctype,
                residency_article=res_art,
                date_of_birth=dob,
                monthly_income_kd=50.0 if nat != "KW" else 150.0,
                phone=f"+965 {random.choice([5, 6, 9])}{random.randint(1000000, 9999999)}",
                address=f"Block {random.randint(1, 12)}, Street {random.randint(1, 90)}, House {random.randint(1, 50)}, Kuwait City",
                is_pep=False
            )
            minors.append(profile.model_dump())
        customers.extend(minors)

        # 4. Generate domestic workers (Article 20)
        doms = []
        for _ in range(100):
            sender_id = f"2850523{random.randint(10, 28)}{random.randint(10000, 99999)}"
            nat = random.choice(["PH", "IN", "BD", "NP", "LK"])
            dob = date(random.randint(1975, 2002), random.randint(1, 12), random.randint(1, 28))
            
            profile = SenderProfile(
                sender_id=sender_id,
                full_name=self.fake.name(),
                nationality=nat,
                customer_type=CustomerType.RESIDENT,
                residency_article="20",
                date_of_birth=dob,
                monthly_income_kd=float(random.randint(60, 90)),
                phone=f"+965 {random.choice([5, 6, 9])}{random.randint(1000000, 9999999)}",
                address=f"Block {random.randint(1, 12)}, Street {random.randint(1, 90)}, House {random.randint(1, 50)}, Salmiya",
                is_pep=False
            )
            doms.append(profile.model_dump())
        customers.extend(doms)

        # 5. Generate Article 22 residents
        art22s = []
        for _ in range(30):
            sender_id = f"2900523{random.randint(10, 28)}{random.randint(10000, 99999)}"
            nat = random.choice(["IN", "EG", "SY", "JO"])
            dob = date(random.randint(1975, 2000), random.randint(1, 12), random.randint(1, 28))
            
            profile = SenderProfile(
                sender_id=sender_id,
                full_name=self.fake.name(),
                nationality=nat,
                customer_type=CustomerType.RESIDENT,
                residency_article="22",
                date_of_birth=dob,
                monthly_income_kd=0.0,  # Dependents - usually declared 0
                phone=f"+965 {random.choice([5, 6, 9])}{random.randint(1000000, 9999999)}",
                address=f"Block {random.randint(1, 12)}, Street {random.randint(1, 90)}, House {random.randint(1, 50)}, Farwaniya",
                is_pep=False
            )
            art22s.append(profile.model_dump())
        customers.extend(art22s)

        # 6. Generate Corporates
        corps = []
        corporate_names = [
            "Al Noor Cleaning Services Co.", "Gulf Maintenance LLC", "Al Tawoos Logistics Co.",
            "Kuwait Trading Corp", "Ahmadi Contracting WLL", "Farwaniya Services Group",
            "Jahra Trading Company", "Capital Investments Co.", "Sands Construction WLL"
        ]
        for cname in corporate_names:
            sender_id = f"100{random.randint(1000000, 9999999)}"
            
            profile = SenderProfile(
                sender_id=sender_id,
                full_name=cname,
                nationality="KW",
                customer_type=CustomerType.CORPORATE,
                date_of_birth=date(2010, 1, 1), # arbitrary placeholder for birth_date
                monthly_income_kd=float(random.randint(5000, 50000)),
                phone=f"+965 2{random.randint(200000, 299999)}",
                address=f"Commercial Tower {random.randint(1, 20)}, Sharq, Kuwait",
                is_pep=False
            )
            corps.append(profile.model_dump())
        customers.extend(corps)

        # 7. Pad the rest with general Kuwaitis and other residents
        n_needed = self.n_customers - len(customers)
        for _ in range(n_needed):
            nat = self._choose_nationality()
            ctype = CustomerType.KUWAITI if nat == "KW" else CustomerType.RESIDENT
            res_art = None
            inc = None
            dob = date(random.randint(1960, 2002), random.randint(1, 12), random.randint(1, 28))

            if ctype == CustomerType.RESIDENT:
                res_art = self._choose_article()
                inc_range = INCOME_RANGES_BY_ARTICLE.get(res_art, (150, 600))
                inc = float(random.randint(inc_range[0], inc_range[1])) if inc_range[1] > 0 else 0.0
            else:
                inc = float(random.randint(1000, 8000))

            sender_id = f"2{random.randint(60, 99)}{random.randint(10, 12)}{random.randint(10, 28)}{random.randint(10000, 99999)}"
            
            profile = SenderProfile(
                sender_id=sender_id,
                full_name=self.fake.name(),
                nationality=nat,
                customer_type=ctype,
                residency_article=res_art,
                date_of_birth=dob,
                monthly_income_kd=inc,
                phone=f"+965 {random.choice([5, 6, 9])}{random.randint(1000000, 9999999)}",
                address=f"Block {random.randint(1, 12)}, Street {random.randint(1, 90)}, House {random.randint(1, 50)}, {random.choice(BRANCHES).split()[0]}",
                is_pep=random.random() < 0.01
            )
            customers.append(profile.model_dump())

        # Convert date_of_birth from date to datetime for MongoDB compatibility
        for c in customers:
            dob = c.get("date_of_birth")
            if isinstance(dob, date) and not isinstance(dob, datetime):
                c["date_of_birth"] = datetime.combine(dob, datetime.min.time())

        self.customers = customers
        return customers

    def generate_transactions(self, customers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Generate Transaction records linked to the customer pool."""
        self.transactions = []
        self.ground_truth = {}
        
        # Filter customer types for specific assignments
        structuring_pool = [c for c in customers if c["customer_type"] == "resident" and c["residency_article"] == "18" and "Shared Building" in (c["address"] or "")]
        tourist_pool = [c for c in customers if c["customer_type"] == "tourist"]
        minor_pool = [c for c in customers if c["customer_type"] == "resident" and c["residency_article"] == "22" and c["monthly_income_kd"] == 50.0] or [c for c in customers if c["customer_type"] == "resident" and c["residency_article"] == "22"]
        dom_pool = [c for c in customers if c["residency_article"] == "20"]
        art22_pool = [c for c in customers if c["residency_article"] == "22" and c["monthly_income_kd"] == 0.0]
        corp_pool = [c for c in customers if c["customer_type"] == "corporate"]
        general_pool = [c for c in customers if c not in structuring_pool + tourist_pool + minor_pool + dom_pool + art22_pool]

        # Calculate exact counts of transactions to generate
        count_a = int(self.n_transactions * 0.02)
        count_b = int(self.n_transactions * 0.03)
        count_c = int(self.n_transactions * 0.02)
        count_d = int(self.n_transactions * 0.02)
        count_e = int(self.n_transactions * 0.01)
        count_f = int(self.n_transactions * 0.01)
        count_g = int(self.n_transactions * 0.01)
        count_h = int(self.n_transactions * 0.005)
        count_i = int(self.n_transactions * 0.02)
        count_j = int(self.n_transactions * 0.005)
        
        total_fraud = count_a + count_b + count_c + count_d + count_e + count_f + count_g + count_h + count_i + count_j
        total_clean = self.n_transactions - total_fraud

        now = datetime.utcnow()
        start_date = now - timedelta(days=365)

        # ----------------------------------------------------
        # PATTERN A — Sanctioned Country (2% of txns)
        # ----------------------------------------------------
        for _ in range(count_a):
            cust = random.choice([c for c in general_pool if c["nationality"] != "IR"])
            ref_no = self._gen_ref_no()
            tx_date = self._random_date(start_date, now)
            amount_kd = float(random.randint(500, 2800))
            txn = self._create_txn_dict(ref_no, cust, amount_kd, "IR", "IRR", "Western Union", tx_date, "general", recipient_is_company=False)
            
            self.transactions.append(txn)
            self.ground_truth[ref_no] = ["SANCTIONED_COUNTRY", "VAGUE_PURPOSE"]
            self.pattern_counts["SANCTIONED_COUNTRY"] += 1
            self.pattern_counts["VAGUE_PURPOSE"] += 1

        # ----------------------------------------------------
        # PATTERN B — Structuring (3% of txns)
        # ----------------------------------------------------
        # Group by building
        groups = {}
        for c in structuring_pool:
            b_id = c["address"].split("Shared Building ")[1].split(",")[0]
            groups.setdefault(b_id, []).append(c)

        acc_num = f"ACC-{random.randint(100000, 999999)}"
        rec_name = self.fake.name()
        
        # We need `count_b` transactions.
        for _ in range(count_b):
            # Pick a group
            b_id = random.choice(list(groups.keys()))
            cust = random.choice(groups[b_id])
            ref_no = self._gen_ref_no()
            
            # Timestamp spread within same week
            base_date = self._random_date(start_date, now)
            tx_date = base_date + timedelta(hours=random.randint(0, 48))
            amount_kd = float(random.randint(400, 900))
            
            txn = self._create_txn_dict(ref_no, cust, amount_kd, cust["nationality"], CURRENCY_MAP.get(cust["nationality"], "USD"), "MoneyGram", tx_date, "Family Support", recipient_is_company=False, acc_number=acc_num, recipient_name=rec_name)
            
            self.transactions.append(txn)
            self.ground_truth[ref_no] = ["STRUCTURING_MULTI_SENDER", "SHARED_IDENTIFIER_NETWORK"]
            self.pattern_counts["STRUCTURING_MULTI_SENDER"] += 1
            self.pattern_counts["SHARED_IDENTIFIER_NETWORK"] += 1

        # ----------------------------------------------------
        # PATTERN C — Income Mismatch (2% of txns)
        # ----------------------------------------------------
        for _ in range(count_c):
            cust = random.choice(dom_pool)
            ref_no = self._gen_ref_no()
            tx_date = self._random_date(start_date, now)
            amount_kd = float(random.randint(300, 600))
            txn = self._create_txn_dict(ref_no, cust, amount_kd, cust["nationality"], CURRENCY_MAP.get(cust["nationality"], "USD"), "Western Union", tx_date, "Family Support", recipient_is_company=False)
            
            self.transactions.append(txn)
            self.ground_truth[ref_no] = ["INCOME_MISMATCH"]
            self.pattern_counts["INCOME_MISMATCH"] += 1

        # ----------------------------------------------------
        # PATTERN D — Non-Home Corridor (2% of txns)
        # ----------------------------------------------------
        non_home_countries = ["RO", "PL", "UA", "CZ"]
        for _ in range(count_d):
            cust = random.choice([c for c in general_pool if c["nationality"] in ["IN", "PH", "EG"]])
            ref_no = self._gen_ref_no()
            tx_date = self._random_date(start_date, now)
            amount_kd = float(random.randint(400, 2000))
            dest = random.choice(non_home_countries)
            txn = self._create_txn_dict(ref_no, cust, amount_kd, dest, "USD", "Western Union", tx_date, "Services", recipient_is_company=False)
            
            self.transactions.append(txn)
            self.ground_truth[ref_no] = ["NON_HOME_CORRIDOR", "VAGUE_PURPOSE"]
            self.pattern_counts["NON_HOME_CORRIDOR"] += 1
            self.pattern_counts["VAGUE_PURPOSE"] += 1

        # ----------------------------------------------------
        # PATTERN E — Article 22 Breach (1% of txns)
        # ----------------------------------------------------
        for _ in range(count_e):
            cust = random.choice(art22_pool)
            ref_no = self._gen_ref_no()
            tx_date = self._random_date(start_date, now)
            amount_kd = float(random.randint(200, 500))  # Single transaction already breaches the 150 KD monthly
            txn = self._create_txn_dict(ref_no, cust, amount_kd, cust["nationality"], CURRENCY_MAP.get(cust["nationality"], "USD"), "MoneyGram", tx_date, "Family Support", recipient_is_company=False)
            
            self.transactions.append(txn)
            self.ground_truth[ref_no] = ["ARTICLE_22_BREACH"]
            self.pattern_counts["ARTICLE_22_BREACH"] += 1

        # ----------------------------------------------------
        # PATTERN F — Corporate Mismatch (1% of txns)
        # ----------------------------------------------------
        for _ in range(count_f):
            cust = random.choice([c for c in corp_pool if c["full_name"] in ["Al Noor Cleaning Services Co.", "Gulf Maintenance LLC"]])
            ref_no = self._gen_ref_no()
            tx_date = self._random_date(start_date, now)
            amount_kd = float(random.randint(800, 2500))
            txn = self._create_txn_dict(ref_no, cust, amount_kd, "IN", "INR", "National Bank of Kuwait", tx_date, "Family Support", recipient_is_company=False, sender_is_corporate=True, sender_company_name=cust["full_name"])
            
            self.transactions.append(txn)
            self.ground_truth[ref_no] = ["CORPORATE_PURPOSE_MISMATCH"]
            self.pattern_counts["CORPORATE_PURPOSE_MISMATCH"] += 1

        # ----------------------------------------------------
        # PATTERN G — Individual to Company (1% of txns)
        # ----------------------------------------------------
        for _ in range(count_g):
            cust = random.choice(general_pool)
            ref_no = self._gen_ref_no()
            tx_date = self._random_date(start_date, now)
            amount_kd = float(random.randint(500, 2800))
            comp_name = random.choice(["Gulf Trading LLC", "Al Manara General Co.", "Al-Ghanim & Sons WLL"])
            txn = self._create_txn_dict(ref_no, cust, amount_kd, "AE", "AED", "Western Union", tx_date, "General Payment", recipient_is_company=True, recipient_company_name=comp_name)
            
            self.transactions.append(txn)
            self.ground_truth[ref_no] = ["INDIVIDUAL_TO_COMPANY", "VAGUE_PURPOSE"]
            self.pattern_counts["INDIVIDUAL_TO_COMPANY"] += 1
            self.pattern_counts["VAGUE_PURPOSE"] += 1

        # ----------------------------------------------------
        # PATTERN H — Minor Sender (0.5% of txns)
        # ----------------------------------------------------
        for _ in range(count_h):
            cust = random.choice(minor_pool)
            ref_no = self._gen_ref_no()
            tx_date = self._random_date(start_date, now)
            amount_kd = float(random.randint(100, 500))
            txn = self._create_txn_dict(ref_no, cust, amount_kd, cust["nationality"], CURRENCY_MAP.get(cust["nationality"], "USD"), "Western Union", tx_date, "Living Expenses", recipient_is_company=False)
            
            self.transactions.append(txn)
            self.ground_truth[ref_no] = ["MINOR_SENDER"]
            self.pattern_counts["MINOR_SENDER"] += 1

        # ----------------------------------------------------
        # PATTERN I — Repeat Flags (2% of txns)
        # ----------------------------------------------------
        # Senders that trigger multiple flags across 30 days. Let's pick 5 repeat offenders
        repeat_offenders = [random.choice(general_pool) for _ in range(5)]
        for _ in range(count_i):
            cust = random.choice(repeat_offenders)
            ref_no = self._gen_ref_no()
            
            # Spread closely in 30 days
            base_date = self._random_date(start_date, now - timedelta(days=30))
            tx_date = base_date + timedelta(days=random.randint(0, 20))
            
            amount_kd = float(random.randint(1500, 2900))
            # Trigger other flags (e.g. Non home corridor or vague purpose)
            dest = "RO" if cust["nationality"] in ["IN", "PH", "EG"] else "IR"
            curr = "USD" if dest == "RO" else "IRR"
            purp = "general" if dest == "IR" else "Services"
            
            txn = self._create_txn_dict(ref_no, cust, amount_kd, dest, curr, "Gulf Bank", tx_date, purp, recipient_is_company=False)
            
            self.transactions.append(txn)
            rules = ["REPEAT_FLAGS", "VAGUE_PURPOSE"]
            if dest == "IR":
                rules.append("SANCTIONED_COUNTRY")
                self.pattern_counts["SANCTIONED_COUNTRY"] += 1
            else:
                rules.append("NON_HOME_CORRIDOR")
                self.pattern_counts["NON_HOME_CORRIDOR"] += 1
            
            self.ground_truth[ref_no] = rules
            self.pattern_counts["REPEAT_FLAGS"] += 1
            self.pattern_counts["VAGUE_PURPOSE"] += 1

        # ----------------------------------------------------
        # PATTERN J — Tourist No POW (0.5% of txns)
        # ----------------------------------------------------
        for _ in range(count_j):
            cust = random.choice(tourist_pool)
            ref_no = self._gen_ref_no()
            tx_date = self._random_date(start_date, now)
            amount_kd = float(random.randint(1000, 3000))
            txn = self._create_txn_dict(
                ref_no, cust, amount_kd, cust["nationality"], CURRENCY_MAP.get(cust["nationality"], "USD"), "Western Union", tx_date, "Family Support",
                recipient_is_company=False, proof_of_wealth=False, proof_of_relationship=False
            )
            
            self.transactions.append(txn)
            self.ground_truth[ref_no] = ["TOURIST_NO_POW"]
            self.pattern_counts["TOURIST_NO_POW"] += 1

        # ----------------------------------------------------
        # CLEAN TRANSACTIONS (85% of txns)
        # ----------------------------------------------------
        clean_purposes = ["Family Support", "Living Expenses", "Medical Expenses", "Education Fees", "Loan Repayment"]
        for _ in range(total_clean):
            cust = random.choice(customers)
            ref_no = self._gen_ref_no()
            tx_date = self._random_date(start_date, now)
            
            # Ensure corporate matches and individual to companies are legitimate/clean
            recipient_is_company = False
            recipient_company_name = None
            sender_is_corporate = cust["customer_type"] == "corporate"
            sender_company_name = cust["full_name"] if sender_is_corporate else None
            
            purpose = random.choice(clean_purposes)
            
            if sender_is_corporate:
                # Legitimate corporate purposes
                purpose = random.choice(["import", "export", "trade", "invoice", "supplier", "procurement"])
                amount_kd = float(random.randint(1500, 15000))
            elif cust["residency_article"] == "20":
                # Domestic workers send small amounts (30-90 KD) monthly
                amount_kd = float(random.randint(30, 90))
            elif cust["residency_article"] == "22":
                # Family dependants under Article 22 send tiny amounts within limits
                amount_kd = float(random.randint(10, 80))
            elif cust["customer_type"] == "tourist":
                # Tourists sending clean transactions
                amount_kd = float(random.randint(50, 900))  # Under the 1000 KD threshold
            else:
                amount_kd = float(random.randint(20, 2800))

            # Non-Kuwaitis send to home country
            dest_country = cust["nationality"]
            if dest_country == "KW" or dest_country == "OTHER":
                dest_country = random.choice(["IN", "PH", "EG", "PK", "US", "GB"])
            elif dest_country == "IR":
                # Clean transactions for Iranians should be very small/rare and to safe currencies if possible, but keep it home
                dest_country = "IR"
                
            # Randomly add corporate recipient for clean individual-to-company (school, hospital, etc.)
            if not sender_is_corporate and random.random() < 0.05:
                recipient_is_company = True
                recipient_company_name = random.choice(["New English School", "Kuwait University", "Al-Salam International Hospital", "Dar Al Shifa Clinic"])
                purpose = "Education Fees" if "School" in recipient_company_name or "University" in recipient_company_name else "Medical Expenses"

            txn = self._create_txn_dict(
                ref_no, cust, amount_kd, dest_country, CURRENCY_MAP.get(dest_country, "USD"), random.choice(BANKS), tx_date, purpose,
                recipient_is_company=recipient_is_company, recipient_company_name=recipient_company_name,
                sender_is_corporate=sender_is_corporate, sender_company_name=sender_company_name,
                proof_of_wealth=(amount_kd >= 1500), proof_of_relationship=(random.random() < 0.8)
            )
            
            self.transactions.append(txn)
            self.ground_truth[ref_no] = []
            self.pattern_counts["CLEAN"] += 1

        # Sort transactions by date so they are in chronological order
        self.transactions.sort(key=lambda x: x["date"])
        return self.transactions

    def generate_ground_truth(self, transactions: List[Dict[str, Any]]) -> Dict[str, List[str]]:
        """Return {ref_no: [rule_ids_that_should_fire]} for evaluation."""
        return self.ground_truth

    def save_to_json(self, output_dir: str = "data/generated/"):
        """Save all three datasets to JSON files."""
        os.makedirs(output_dir, exist_ok=True)
        
        # Helper to convert dates & datetimes to strings
        def _json_serial(obj):
            if isinstance(obj, (datetime, date)):
                return obj.isoformat()
            raise TypeError(f"Type {type(obj)} not serializable")

        with open(os.path.join(output_dir, "customers.json"), "w") as f:
            json.dump(self.customers, f, default=_json_serial, indent=2)

        with open(os.path.join(output_dir, "transactions.json"), "w") as f:
            json.dump(self.transactions, f, default=_json_serial, indent=2)

        with open(os.path.join(output_dir, "ground_truth.json"), "w") as f:
            json.dump(self.ground_truth, f, indent=2)

    def load_to_mongodb(self, db) -> Dict[str, int]:
        """Insert all records into MongoDB. Returns counts per collection."""
        # Convert date & datetime fields into true python dates for pymongo
        # (they are already in native python format in lists, so direct insert is fine!)
        customers_col = db["customers"]
        transactions_col = db["transactions"]
        
        # Clear existing collection data to ensure fresh seeding
        customers_col.delete_many({})
        transactions_col.delete_many({})

        # Bulk insert
        if self.customers:
            customers_col.insert_many(self.customers)
        if self.transactions:
            transactions_col.insert_many(self.transactions)

        return {
            "customers": len(self.customers),
            "transactions": len(self.transactions)
        }

    # ----------------------------------------------------
    # Private Helpers
    # ----------------------------------------------------
    def _choose_nationality(self) -> str:
        nats = list(SENDER_NATIONALITY_DISTRIBUTION.keys())
        w = list(SENDER_NATIONALITY_DISTRIBUTION.values())
        return random.choices(nats, weights=w, k=1)[0]

    def _choose_article(self) -> str:
        arts = list(ARTICLE_DISTRIBUTION.keys())
        w = list(ARTICLE_DISTRIBUTION.values())
        return random.choices(arts, weights=w, k=1)[0]

    def _random_date(self, start: datetime, end: datetime) -> datetime:
        delta = end - start
        int_delta = (delta.days * 24 * 60 * 60) + delta.seconds
        random_second = random.randint(0, int_delta)
        return start + timedelta(seconds=random_second)

    def _gen_ref_no(self) -> str:
        return f"TXN{random.randint(100000000, 999999999)}"

    def _create_txn_dict(
        self, ref_no: str, customer: Dict[str, Any], amount_kd: float, dest_country: str,
        currency: str, bank: str, tx_date: datetime, purpose: str,
        recipient_is_company: bool = False, recipient_company_name: str = None,
        sender_is_corporate: bool = False, sender_company_name: str = None,
        proof_of_wealth: bool = False, proof_of_relationship: bool = False,
        acc_number: str = None, recipient_name: str = None
    ) -> Dict[str, Any]:
        
        rate = FX_RATES.get(currency, 3.25)
        amount_orig = round(amount_kd * rate, 2)
        
        # Build Pydantic model for validation
        txn_model = Transaction(
            ref_no=ref_no,
            amount=amount_orig,
            currency=currency,
            amount_kd=amount_kd,
            bank=bank,
            date=tx_date,
            acc_number=acc_number or f"ACC-{random.randint(100000, 999999)}",
            sender_id=customer["sender_id"],
            sender_name=customer["full_name"],
            sender_nationality=customer["nationality"],
            sender_tel=customer["phone"],
            branch=random.choice(BRANCHES),
            recipient_name=recipient_name or self.fake.name(),
            recipient_country=dest_country,
            recipient_is_company=recipient_is_company,
            recipient_company_name=recipient_company_name,
            transaction_purpose=purpose,
            sender_is_corporate=sender_is_corporate,
            sender_company_name=sender_company_name,
            proof_of_wealth_provided=proof_of_wealth,
            proof_of_relationship_provided=proof_of_relationship
        )
        return txn_model.model_dump()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="EagleEyes Synthetic Data Generator CLI")
    parser.add_argument("--customers", type=int, default=500, help="Number of customers to generate")
    parser.add_argument("--transactions", type=int, default=5000, help="Number of transactions to generate")
    parser.add_argument("--mongo", action="store_true", help="Insert records into local MongoDB database")
    args = parser.parse_args()

    console = Console()
    console.print("[bold blue]EagleEyes[/bold blue] — Initializing Data Generation Sequence...")

    generator = DataGenerator(n_customers=args.customers, n_transactions=args.transactions)
    
    console.print(f"Generating [bold green]{args.customers}[/bold green] Sender Profiles...")
    customers = generator.generate_customers()
    
    console.print(f"Generating [bold green]{args.transactions}[/bold green] Remittance Transactions...")
    transactions = generator.generate_transactions(customers)
    
    # Save datasets
    console.print("Writing datasets to JSON files...")
    generator.save_to_json()
    
    # Optional MongoDB load
    if args.mongo:
        from pymongo import MongoClient
        from core.config import settings
        
        console.print(f"Connecting to MongoDB at: [dim]{settings.MONGODB_URI}[/dim]...")
        try:
            client = MongoClient(settings.MONGODB_URI)
            db = client[settings.MONGODB_DB_NAME]
            counts = generator.load_to_mongodb(db)
            console.print(f"[bold green]Seeded MongoDB successfully![/bold green] Loaded {counts['customers']} customers, {counts['transactions']} transactions.")
        except Exception as e:
            console.print(f"[bold red]Failed to load into MongoDB:[/bold red] {e}")

    # Build Summary Table
    table = Table(title="EagleEyes AML Synthetic Data Summary", title_style="bold magenta")
    table.add_column("Dataset Attribute", style="cyan")
    table.add_column("Count / Value", justify="right", style="green")

    table.add_row("Total Customers Generated", f"{len(customers)}")
    table.add_row("Total Transactions Generated", f"{len(transactions)}")
    table.add_row("Clean Transactions", f"{generator.pattern_counts['CLEAN']}")
    
    table.add_section()
    table.add_row("Seeded Pattern: Sanctioned Country (A)", f"{generator.pattern_counts['SANCTIONED_COUNTRY']}")
    table.add_row("Seeded Pattern: Structuring (B)", f"{generator.pattern_counts['STRUCTURING_MULTI_SENDER']}")
    table.add_row("Seeded Pattern: Income Mismatch (C)", f"{generator.pattern_counts['INCOME_MISMATCH']}")
    table.add_row("Seeded Pattern: Non-Home Corridor (D)", f"{generator.pattern_counts['NON_HOME_CORRIDOR']}")
    table.add_row("Seeded Pattern: Article 22 Breach (E)", f"{generator.pattern_counts['ARTICLE_22_BREACH']}")
    table.add_row("Seeded Pattern: Corporate Mismatch (F)", f"{generator.pattern_counts['CORPORATE_PURPOSE_MISMATCH']}")
    table.add_row("Seeded Pattern: Individual to Company (G)", f"{generator.pattern_counts['INDIVIDUAL_TO_COMPANY']}")
    table.add_row("Seeded Pattern: Minor Sender (H)", f"{generator.pattern_counts['MINOR_SENDER']}")
    table.add_row("Seeded Pattern: Repeat Flags (I)", f"{generator.pattern_counts['REPEAT_FLAGS']}")
    table.add_row("Seeded Pattern: Tourist No POW (J)", f"{generator.pattern_counts['TOURIST_NO_POW']}")

    console.print("\n")
    console.print(table)
    console.print("\n[bold green]Data Generation Cycle Complete![/bold green] All assets written to [cyan]data/generated/[/cyan].\n")
