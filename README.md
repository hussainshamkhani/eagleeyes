<<<<<<< HEAD
# EagleEyes — Autonomous Self-Improving AML Platform

**EagleEyes** is an open-source, automated compliance reasoning and Anti-Money Laundering (AML) platform. It is designed to help money exchange companies (specifically matching Kuwait-based regulations) automatically screen transactions, write compliance justifications, and learn to improve itself over time.

This agent was developed using **MongoDB** as its primary database and **Arize Phoenix** for tracking, tracing, and performance evaluations.

---

## 🌟 How EagleEyes Works

When a customer makes a money transfer:
1. **Rule Screening**: EagleEyes runs the transaction through **12 rules** (such as visa restrictions, income mismatches, and tourist limits).
2. **Risk Calculation**: If any rules trigger, a risk score is calculated. If the risk is high enough, the transaction is flagged.
3. **AI Reasoning**: A smart compliance agent powered by **Gemini 3.5 Flash** (via the Google Agent Development Kit) analyzes the details, decides on an action (Clear, Monitor, Hold, or File STR), and writes a detailed compliance report.
4. **Self-Improvement**: As the agent runs and humans review the results, EagleEyes queries its own performance logs in **Arize Phoenix** to recommend updates to rule weights and thresholds to make future alerts more accurate.

---

## 🛠️ Tech Stack & Key Components

- **MongoDB / Motor**: The database used to store customer profiles, transactions, alerts, and dynamic configuration weights.
- **Arize Phoenix**: The open-source observability framework used to monitor the agent, capture detailed execution traces, and run LLM-as-a-Judge evaluations.
- **Google Agent Development Kit (ADK)**: Orchestrates the Gemini AI agent.
- **FastAPI**: The backend web framework providing APIs and serving the compliance console.
- **HTML/CSS Dashboard**: An interactive, glassmorphic UI to manage alerts, view reports, and deploy updates.

---

## 🚀 Quick Start & Local Setup

Getting EagleEyes running on your local machine is quick and straightforward.

### 1. Prerequisites
Ensure you have the following installed:
- **Python 3.12+**
- **Docker Desktop** (to run MongoDB)
- A **Gemini API Key** (from Google AI Studio or Google Cloud)

### 2. Clone the Repository & Configure Environment
Copy the blueprint configuration file:
```bash
cp .env.example .env
```
Open `.env` and fill in your credentials:
- `GEMINI_API_KEY`: Your Gemini API key.
- `MONGODB_URI`: Leave as `mongodb://localhost:27017` (Docker MongoDB default).
- `ENVIRONMENT`: Set to `development`. In development mode, the application will automatically spin up a local Arize Phoenix instance for telemetry tracing.

### 3. Set Up Virtual Environment & Dependencies
Create a virtual environment and install the required packages:

**On Windows:**
```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

**On macOS / Linux:**
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 4. Start MongoDB
Run the database container in the background:
```bash
docker-compose up -d
```

### 5. Seed Sample Data
Populate MongoDB with 100 mock customer accounts and 600 transactions containing realistic regulatory compliance triggers:
```bash
python -m data.generator --mongo
```

### 6. Run the Application
Start the FastAPI server:
```bash
uvicorn main:app --reload
```

Now open your browser and navigate to:
**`http://localhost:8000/`**

This will automatically open the interactive **EagleEyes Compliance Dashboard**.

---

## 🖥️ Using the Dashboard

Once you open the web console, you can explore the three main sections:

1. **Dashboard & Alert Queue**:
   - View key metrics like total transactions checked, pending alerts, and filed Suspicious Transaction Reports (STRs).
   - Select pending alerts to review the customer KYC details, the rule violations, and the Gemini AI compliance justification.
   - Choose a compliance action (Clear, Monitor, Hold, or File STR).
   
2. **Monospace STR Viewer**:
   - When you click **File STR** (or for high-risk alerts that auto-file), the platform formats a formal Suspicious Transaction Report.
   - Click the "View Filed STR Report" button to copy or read the plain-text document designed for submission to financial regulators.

3. **MLOps Self-Improvement Hub**:
   - Navigate to the **Self-Improvement** tab.
   - After evaluating transactions, the Gemini meta-analysis agent compares baseline rule weights against recommended adjustments based on performance logs in Arize Phoenix.
   - Review and deploy weight calibrations or new keyword restrictions live to MongoDB with a single click.

4. **Sandbox Console**:
   - Use the developer buttons at the bottom of the page to reset the database or run a simulated **Batch Evaluation** to watch the transactions and self-improvement loops run in real-time.

---

## 📂 Project Structure

```text
eagle-eyes/
├── main.py                     # Main application entry point
├── requirements.txt            # Python library dependencies
├── docker-compose.yml          # MongoDB Docker services config
├── .env.example                # Template for environment variables
├── core/                       # App configuration and constants
├── data/                       # KYC/transaction schemas and data generator
├── db/                         # MongoDB connection layer
├── rules/                      # The 12 compliance rules logic
├── agent/                      # Gemini ADK agent and self-improvement loop
├── integrations/               # Arize Phoenix telemetry and MCP tools
├── reports/                    # Regulatory STR report generator
└── ui/                         # HTML & CSS dashboard files
```

---

## 🧪 Running Tests
To verify that the database connection, caching, rules engine, and self-improvement modules are working correctly, run:
```bash
python scratch/test_self_improvement.py
```

---

## 📄 License
This project is licensed under the MIT License.
=======
# eagleeyes
>>>>>>> f2e153ee63b35615e6703343272ed0fd55335d96
