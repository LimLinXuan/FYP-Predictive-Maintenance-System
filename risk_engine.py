
from datetime import datetime


HIGH_RISK_THRESHOLD = 65

PROBABILITY_SAFETY_FLOOR = [
    (0.90, 90),
    (0.80, 80),
    (0.70, 70),
    (0.50, 50),
]


def compute_risk_score(prob, row):
    """
    Combine ML failure probability, engineering rules,
    and anomaly signals into a risk score from 0 to 100.
    """
    points = 0
    reasons = []
    override_applied = False

    # Layer 1 — ML Probability (0–50)
    ml_pts = round(prob * 50)
    points += ml_pts

    if prob >= 0.65:
        reasons.append(f"High failure probability ({prob:.0%})")
    elif prob >= 0.35:
        reasons.append(f"Elevated failure probability ({prob:.0%})")

    # Layer 2 — Domain Knowledge
    if row.get("hdf_risk", 0):
        points += 15
        reasons.append(
            "Heat dissipation failure condition (low temp_diff + low rpm)"
        )

    if row.get("pwf_risk", 0):
        points += 10
        reasons.append(
            "Power failure condition (power out of safe range)"
        )

    if row.get("osf_risk", 0):
        points += 15
        reasons.append(
            "Overstrain failure condition (tool_torque threshold exceeded)"
        )

    # Layer 3 — Anomaly Detection
    if row.get("isoforest_anomaly", 0):
        points += 12
        reasons.append("Isolation Forest anomaly detected")

    if row.get("zscore_anomaly", 0):
        points += 8
        reasons.append("Z-score anomaly detected")

    points = min(points, 100)

    # Probability Safety Floor
    floor = 0

    for prob_threshold, floor_value in PROBABILITY_SAFETY_FLOOR:
        if prob >= prob_threshold:
            floor = floor_value
            break

    if points < floor:
        original_points = points
        points = floor
        override_applied = True
        reasons.append(
            f"Probability safety floor applied: score adjusted "
            f"from {original_points} to {floor} per risk policy, "
            f"since predicted failure probability ({prob:.0%}) "
            f"guarantees a minimum risk level regardless of "
            f"rule/anomaly signal count"
        )

    # Final classification
    if points >= 85:
        level = "Critical"
        action = "Immediate shutdown inspection required"
    elif points >= HIGH_RISK_THRESHOLD:
        level = "High"
        action = "Schedule inspection within 4 hours"
    elif points >= 35:
        level = "Medium"
        action = "Monitor closely, inspect within 24 hours"
    else:
        level = "Low"
        action = "Normal operation"

    return {
        "risk_score": points,
        "risk_level": level,
        "action": action,
        "reasons": " | ".join(reasons) if reasons else "No risk signals",
        "prob": round(float(prob), 4),
        "override_applied": override_applied,
        "timestamp": datetime.now().isoformat(),
    }
