# SOURCE_OF_TRUTH.md

## Intelligent Machine Performance Monitoring and Anomaly Detection System with Failure Prediction for Industrial Production Lines

**Author:** Lim Lin Xuan  
**Coventry University ID:** 15126494

---

# 1. Purpose of This Document

This document is the canonical technical reference for the current implementation of the Final Year Project (FYP):

> **Intelligent Machine Performance Monitoring and Anomaly Detection System with Failure Prediction for Industrial Production Lines**

Its purpose is to establish the confirmed technical facts of the current system and to prevent contradictions between:

- the current software implementation,
- academic documentation,
- previous project descriptions,
- future revisions,
- and generated explanations.

This document describes the **current confirmed implementation**, not an earlier design proposal and not a planned future system.

---

# 2. Source-of-Truth Authority

When technical information conflicts, use the following authority order:

1. **Current GitHub implementation**
2. **This SOURCE_OF_TRUTH.md**
3. Other directly confirmed project evidence
4. Academic documentation such as the DPP
5. Assumptions or inferred behaviour

The DPP is an academic document and may represent an earlier stage of the project. It must not override the current implementation.

If the DPP conflicts with the current implementation:

- do not change the implementation merely to match the DPP;
- identify the conflict;
- describe the actual implementation accurately;
- update the academic documentation only when explicitly requested.

---

# 3. No-Invention Rule

Technical claims must be supported by the current implementation or confirmed project evidence.

Do not invent:

- algorithms,
- API endpoints,
- database tables,
- database fields,
- model behaviour,
- model metrics,
- risk calculations,
- runtime architecture,
- frontend technologies,
- sensor integrations,
- hardware integrations,
- deployment behaviour,
- experimental results,
- or performance values.

If a fact cannot be confirmed, explicitly state that it is not confirmed.

Do not infer runtime behaviour simply because a component exists in a notebook, documentation, diagram, or earlier design.

---

# 4. Current System Scope

The project focuses on:

- machine performance monitoring,
- anomaly detection,
- failure prediction,
- risk scoring,
- explainability,
- alert and decision management,
- dashboard visualisation,
- historical streaming simulation,
- and supporting administrative/audit functions.

The system is based on the **AI4I 2020 Predictive Maintenance Dataset**.

Confirmed dataset baseline:

- 10,000 records
- 14 columns

The project is a software-based predictive maintenance prototype.

It is **not currently connected to physical industrial sensors, IoT devices, or production-line hardware**.

---

# 5. Current Technology Stack

## Backend

- Python
- Flask
- SQLite
- Flasgger / Swagger UI

## Frontend

The current frontend is:

- Flask-rendered Jinja2 HTML templates
- Vanilla JavaScript
- Tailwind CSS
- Chart.js

### Important

React is **not part of the current implementation**.

There is no current React/JSX/TSX/Vite application in the confirmed implementation.

Therefore, the following descriptions are outdated when referring to the current implementation:

- React frontend
- React dashboard
- React-based dashboard
- React application

---

# 6. Current Application Architecture

The current system separates the machine-learning pipeline from the Flask web application.

Conceptually:

```text
AI / ML Pipeline
        |
        |  preprocessing
        |  feature engineering
        |  anomaly detection
        |  failure prediction
        |  risk calculation
        |  SHAP generation
        v
Persisted Results / SQLite
        |
        v
Flask Application
        |
        |-- REST/API endpoints
        |-- authentication
        |-- RBAC
        |-- alerts
        |-- notifications
        |-- audit functions
        |-- health monitoring
        |-- CSV export
        |-- streaming simulation
        v
Jinja2 / JavaScript Frontend
        |
        |-- Dashboard
        |-- Records
        |-- Alerts
        |-- Machine Detail
        |-- Explainability
        |-- Metrics
        |-- Model Comparison
        |-- Streaming
        |-- Administration

The current Flask application primarily consumes persisted machine-learning and decision results.

Do not describe the current Flask application as a complete live ML inference engine unless the relevant runtime inference behaviour is directly confirmed in the implementation.

7. Machine Learning Pipeline

The project contains a machine-learning development pipeline documented in the project notebook/materials.

The confirmed ML workflow includes:

Exploratory Data Analysis
Feature Engineering
Anomaly Detection
Failure Prediction
Model Evaluation
Risk/Decision Processing
SHAP Explainability
Persistence of results for application use
8. Feature Engineering

Confirmed engineered features include:

temp_diff
power
tool_torque
tool_wear_index
rpm_torque_ratio
rolling/statistical features where implemented

Feature engineering descriptions must reflect the actual implementation rather than earlier proposed designs.

9. Anomaly Detection

Two anomaly-detection approaches are part of the confirmed ML pipeline:

9.1 Z-score

Z-score based detection is used to identify statistically abnormal sensor/feature values.

9.2 Isolation Forest

Isolation Forest is used as an unsupervised anomaly-detection method.

The anomaly results contribute to the decision/risk layer where confirmed by the implementation.

10. Failure Prediction

The project uses binary machine-failure prediction.

Confirmed models include:

Logistic Regression

Used as the baseline/comparison classifier.

Random Forest

Used as the current production/current deployed inference model.

The distinction is important:

Logistic Regression
= baseline / comparison model

Random Forest
= current production / deployed inference model

Do not describe Logistic Regression as the current deployed production model.

11. Class Imbalance Handling

SMOTE is used in the machine-learning pipeline for handling class imbalance where confirmed by the implementation.

Descriptions of the training pipeline should distinguish preprocessing/training steps from runtime Flask behaviour.

12. Model Evaluation

Confirmed evaluation includes metrics such as:

Accuracy
Precision
Recall
F1-score
ROC-AUC
classification reports
threshold analysis/sweeps where implemented

A confirmed Random Forest ROC-AUC value is:

ROC-AUC = 0.970

The recorded model comparison also contains a Logistic Regression ROC-AUC value of approximately:

ROC-AUC = 0.990

These values must not be used to incorrectly claim that Logistic Regression is the production model.

Random Forest remains the current production/deployed inference model.

Important

Do not invent or state exact final Accuracy, Precision, Recall, or F1 values unless they are directly confirmed by the relevant project evidence.

Acceptance criteria such as:

F1 ≥ 0.85
ROC-AUC ≥ 0.90

must be described as criteria/requirements unless an achieved result is independently confirmed.

13. SHAP Explainability

SHAP is used to provide explainability for the Random Forest production model.

The confirmed implementation uses:

shap.TreeExplainer(rf)

SHAP is generated as part of the ML pipeline and persisted for application/dashboard consumption.

The current architecture is conceptually:

Random Forest
      |
      v
SHAP TreeExplainer
      |
      v
Persisted SHAP results/artifacts
      |
      v
Flask/API
      |
      v
Dashboard Explainability
Important

The current Flask streaming/dashboard flow must not be described as synchronously recalculating SHAP for every streamed machine record.

SHAP is not confirmed as being recomputed client-side.

The dashboard consumes generated/persisted SHAP results.

14. Risk Engine

The current system uses a risk-scoring / decision layer that combines multiple sources of information.

The risk score is not simply a normalised machine-failure probability.

The confirmed decision logic contains:

14.1 ML Probability Component

The ML probability contributes to the risk score.

The implementation includes a probability-derived component based on:

round(probability × 50)

where confirmed by the implementation.

14.2 Domain / Rule Component

Confirmed domain-related risk contributions include:

HDF risk: +15
PWF risk: +10
OSF risk: +15
14.3 Anomaly Component

Confirmed anomaly contributions include:

Isolation Forest: +12
Z-score: +8

The exact application of individual components must follow the current implementation.

15. Probability Safety Floor

The risk engine includes a probability safety-floor mechanism.

The confirmed minimum-risk mappings include:

Predicted Failure Probability	Minimum Risk Score
≥ 0.90	90
≥ 0.80	80
≥ 0.70	70
≥ 0.50	50

This mechanism prevents a high predicted failure probability from resulting in an artificially low final risk score.

Where applicable, the decision record can indicate whether an override/safety-floor mechanism was applied.

16. Risk Thresholds and Levels

The confirmed high-risk threshold is:

HIGH_RISK_THRESHOLD = 65

The current risk classification contains four levels:

Risk Score	Risk Level
< 35	Low
35–64	Medium
65–84	High
≥ 85	Critical

Therefore, the current implementation must not be described as having only:

Low / Medium / High

The implemented classification is:

Low / Medium / High / Critical

17. Decision Log

decision_log is an important persistence layer for the system's machine-level decisions.

The decision information can include values such as:

machine/record identification
prediction probability
risk score
risk level
action
reasons
override information

The confirmed implementation stores the Random Forest prediction probability in the decision records used by the application.

The decision log should therefore be treated as an important persisted representation of the system's generated decision/risk result.

18. Database

The runtime database is:

machine_monitor.db

The database is SQLite-based.

The confirmed current application database contains the following tables:

users
notifications
audit_log
health_log
machine_data
predictions
decision_log
shap_values
Important naming rule

The current table is:

predictions

not:

prediction

Any academic documentation referring to prediction as the current table name should be corrected when updating the DPP.

19. Database Separation

The current database contains runtime/application data.

The database file itself is not the canonical source of the software implementation.

The GitHub repository should primarily contain:

source code,
templates,
static assets,
documentation,
configuration examples/scripts where appropriate.

The runtime SQLite database should not automatically be treated as source code or as the authoritative technical specification.

The database schema/documentation should describe the structure separately from the runtime database file.

20. Authentication and Authorisation

The current application includes authentication and role-based access control.

Confirmed roles include:

Admin
Technician

The application also includes forced password-change functionality for the initial/default admin setup.

The users table supports the authentication/user-management functionality.

21. Audit Logging

The application contains audit logging functionality.

The current database includes:

audit_log

The system also includes decision logging through:

decision_log

These must not be treated as the same concept unless the implementation explicitly connects them.

22. Notifications and Alerts

The current application includes:

notifications
alerts
alert assignment
alert resolution
false-positive handling

The current database contains:

notifications

These are part of the current application functionality.

23. Health Monitoring

The current application includes health-monitoring functionality.

The database contains:

health_log

Health monitoring should be distinguished from machine-failure prediction and risk scoring.

24. Streaming

The current streaming feature is a simulation using historical machine records.

It does not represent live industrial sensor acquisition.

The current implementation can provide browser-side/dashboard updates using application streaming mechanisms, including SSE where implemented.

The correct terminology is:

historical-data streaming simulation

or:

simulated real-time dashboard updates

Avoid claiming:

live industrial sensor monitoring

or:

real-time physical machine sensing

because there is currently no confirmed physical sensor/IoT integration.

25. API / Backend

The Flask application provides the backend/API layer for the dashboard.

Confirmed backend/application functionality includes areas such as:

machine records
machine details
predictions/results
risk/decision information
alerts
SHAP/explainability
notifications
health monitoring
authentication
role-based access control
user management
audit logging
CSV export
streaming
API documentation

The exact endpoint names and routes must be taken from the current app.py implementation.

Do not invent endpoint names.

26. Frontend / Dashboard

The current dashboard is served through Flask/Jinja2 templates.

Confirmed frontend technologies:

Jinja2
Vanilla JavaScript
Tailwind CSS
Chart.js

Current application pages/features include areas such as:

Dashboard
Records / All Records
Alerts
Machine Detail
Explainability / SHAP
Metrics
Model Comparison
Streaming
Notifications
Audit Log
Manage Users
Health
Login
Change Password

The exact page behaviour must be derived from the current templates and Flask routes.

27. CSV Export

The current application includes CSV export functionality.

CSV export should be described as an application/reporting feature rather than as part of the machine-learning training pipeline.

28. Conceptual Architecture vs Actual Runtime Architecture

Academic UML diagrams and architecture diagrams may contain conceptual components such as:

MLModel
RandomForest
IsolationForest
StreamSession
MetricsSummary
Prediction
SHAPValues

These may be acceptable as conceptual/design models.

However, they must not be presented as proof that the Flask runtime contains corresponding Python class hierarchies or runtime ML components unless those classes actually exist in the implementation.

The distinction is:

Conceptual architecture
≠
Actual source-code architecture

Academic diagrams should be labelled and explained appropriately.

29. Current ML/Application Boundary

The current project should distinguish between:

ML Pipeline

Responsible for activities such as:

data processing
feature engineering
anomaly detection
model training/evaluation
Random Forest inference
risk calculation
SHAP generation
persistence of results
Flask Application

Responsible for activities such as:

authentication
user management
API/backend services
reading persisted machine/decision results
dashboard rendering
alerts
notifications
audit functions
health monitoring
streaming simulation
CSV export
serving SHAP/explainability results

This boundary is important when describing the system architecture.

30. Real-Time Terminology

The project should consistently distinguish:

Implemented
historical-data replay
simulated streaming
browser/dashboard updates
SSE-based application updates where implemented
Not implemented
physical industrial sensors
IoT sensor ingestion
live production-line machine telemetry
hardware-connected predictive maintenance

Therefore, preferred academic wording is:

simulated real-time monitoring using historical machine records

rather than:

real-time industrial machine monitoring

unless the limitation is explicitly stated.

31. Current Production Model vs Best Evaluation Metric

Do not equate:

highest ROC-AUC

with:

production model

The current recorded comparison contains:

Logistic Regression ROC-AUC ≈ 0.990
Random Forest ROC-AUC ≈ 0.970

Nevertheless:

Random Forest is the current production/deployed inference model.

SHAP is implemented for Random Forest because it is the production model, not because it should automatically be described as the highest-scoring model by every metric.

32. Academic Documentation Rules

The DPP is an earlier academic snapshot and may contain outdated descriptions.

When updating the DPP:

Correct current implementation descriptions

Use:

Flask + Jinja2 + JavaScript + Tailwind + Chart.js
offline/precomputed ML pipeline where applicable
Random Forest as production/deployed inference model
Logistic Regression as baseline/comparison
persisted SHAP results
four risk levels
fused risk engine
probability safety floor
current eight-table database
historical streaming simulation
Avoid
React as current frontend
Flask runtime ML inference unless directly confirmed
synchronous per-row SHAP during streaming
three-level risk classification
probability-only risk score definition
prediction as the current table name
live industrial sensor monitoring
unsupported online inference claims
invented metric values
33. Known DPP Update Priorities

The following areas of the older DPP require particular attention when it is eventually updated:

React frontend references
ML runtime/inference architecture diagrams
SHAP streaming/in-line computation descriptions
Three-level risk classification
Risk-score definition
Database table names and database architecture
Online sensor-record inference claims
"Real-time" terminology
Production-model wording
Risk-engine formula
Probability safety-floor mechanism
decision_log role
SHAP storage architecture
Current authentication/RBAC/notification/audit functionality
Current API/backend coverage

This list is a documentation update guide, not itself evidence of implementation.

34. Implementation Status Terminology

When discussing any feature, classify it using one of these categories:

IMPLEMENTED

Confirmed in the current source code or directly confirmed evidence.

DOCUMENTED

Described in academic/project documentation but not necessarily confirmed as current implementation.

PLANNED / PROPOSED

A future or intended feature that is not confirmed as implemented.

EXPERIMENTAL / NOTEBOOK-ONLY

Present in notebooks or experiments but not necessarily integrated into the Flask application.

NOT IMPLEMENTED

Explicitly known not to exist in the current implementation.

Never convert:

DOCUMENTED

into:

IMPLEMENTED

without evidence.

35. Evidence and Uncertainty Rules

When explaining a technical fact:

Prefer direct source-code evidence.
Use exact variable/table/function names when confirmed.
Distinguish stored results from runtime computation.
Distinguish notebook functionality from Flask functionality.
Distinguish conceptual diagrams from actual classes/components.
Distinguish evaluation criteria from achieved results.

If evidence is insufficient, say:

Not confirmed by the current implementation/evidence.

Do not fill the gap with assumptions.

36. Change-Control Rule

This document should only be changed when there is evidence that the current implementation or confirmed project facts have changed.

If the source code changes:

inspect the change;
determine whether it affects a canonical technical fact;
update this document if necessary;
then update academic documentation if required.

Do not update this document merely because the DPP uses different terminology.

37. Final Canonical Summary

The current FYP is a predictive-maintenance software prototype based on the AI4I 2020 dataset.

The current implementation consists of:

AI/ML pipeline
    |
    |-- Feature engineering
    |-- Z-score anomaly detection
    |-- Isolation Forest
    |-- Logistic Regression baseline
    |-- Random Forest production inference
    |-- Risk engine
    |-- SHAP explainability
    |
    v
Persisted results / SQLite
    |
    v
Flask backend/API
    |
    |-- Authentication/RBAC
    |-- Records
    |-- Alerts
    |-- Notifications
    |-- Audit logging
    |-- Health monitoring
    |-- CSV export
    |-- Streaming simulation
    |
    v
Jinja2 + JavaScript + Tailwind + Chart.js dashboard

The most important locked facts are:

Frontend: Flask/Jinja2 + vanilla JavaScript + Tailwind CSS + Chart.js

Production inference model: Random Forest

Baseline/comparison model: Logistic Regression

Anomaly detection: Z-score + Isolation Forest

Explainability: SHAP using Random Forest / TreeExplainer(rf)

Risk threshold: HIGH_RISK_THRESHOLD = 65

Risk levels: Low / Medium / High / Critical

Risk engine: fused ML probability + domain/rule factors + anomaly factors + probability safety floor

Probability safety floor: 0.90→90, 0.80→80, 0.70→70, 0.50→50

Database: SQLite machine_monitor.db

Current database tables: users, notifications, audit_log, health_log, machine_data, predictions, decision_log, shap_values

Streaming: historical-data streaming simulation, not physical real-time sensor integration

SHAP: generated/persisted for dashboard consumption, not synchronously recalculated per streamed row by Flask

Academic documentation: DPP is an earlier snapshot and must be checked against this current implementation before being treated as technically current.

38. Canonical Priority Statement

For all future FYP-related technical questions:

The current GitHub implementation defines what the system actually does.

This SOURCE_OF_TRUTH.md defines confirmed technical facts and locked terminology.

The DPP describes the academic documentation and must be updated when it becomes inconsistent with the confirmed current implementation.

Never modify the implementation merely to make it match outdated academic documentation.

Never invent implementation details merely to make the DPP appear complete.