# Database Schema

- **Database:** `machine_monitor.db`
- **Tables:** 14

## `assets`

| Column | Type | Not Null | Default | Primary Key | Primary Key Order |
|---|---|---|---|---|---|
| `asset_id` | `TEXT` | No | `NULL` | Yes | 1 |
| `machine_id` | `TEXT` | Yes | `NULL` | No | 0 |
| `machine_name` | `TEXT` | Yes | `NULL` | No | 0 |
| `machine_type` | `TEXT` | No | `NULL` | No | 0 |
| `line_id` | `TEXT` | Yes | `NULL` | No | 0 |
| `location` | `TEXT` | No | `NULL` | No | 0 |
| `manufacturer` | `TEXT` | No | `NULL` | No | 0 |
| `model` | `TEXT` | No | `NULL` | No | 0 |
| `criticality` | `TEXT` | Yes | `'MEDIUM'` | No | 0 |
| `status` | `TEXT` | Yes | `'RUNNING'` | No | 0 |

### Foreign Keys

| Column | References Table | References Column | On Update | On Delete |
|---|---|---|---|---|
| `line_id` | `production_lines` | `line_id` | NO ACTION | NO ACTION |

### Indexes

| Index Name | Unique | Origin | Partial |
|---|---|---|---|
| `sqlite_autoindex_assets_2` | Yes | u | No |

Indexed columns: `machine_id`
| `sqlite_autoindex_assets_1` | Yes | pk | No |

Indexed columns: `asset_id`

**Row count:** 20

---

## `audit_log`

| Column | Type | Not Null | Default | Primary Key | Primary Key Order |
|---|---|---|---|---|---|
| `id` | `INTEGER` | No | `NULL` | Yes | 1 |
| `timestamp` | `TEXT` | Yes | `NULL` | No | 0 |
| `username` | `TEXT` | Yes | `NULL` | No | 0 |
| `action` | `TEXT` | Yes | `NULL` | No | 0 |
| `target` | `TEXT` | No | `NULL` | No | 0 |
| `details` | `TEXT` | No | `NULL` | No | 0 |

### Foreign Keys

None declared.

### Indexes

None declared.

**Row count:** 388

---

## `decision_log`

| Column | Type | Not Null | Default | Primary Key | Primary Key Order |
|---|---|---|---|---|---|
| `air_temp` | `REAL` | No | `NULL` | No | 0 |
| `process_temp` | `REAL` | No | `NULL` | No | 0 |
| `rpm` | `INTEGER` | No | `NULL` | No | 0 |
| `torque` | `REAL` | No | `NULL` | No | 0 |
| `tool_wear` | `INTEGER` | No | `NULL` | No | 0 |
| `temp_diff` | `REAL` | No | `NULL` | No | 0 |
| `power` | `REAL` | No | `NULL` | No | 0 |
| `tool_torque` | `REAL` | No | `NULL` | No | 0 |
| `tool_wear_index` | `REAL` | No | `NULL` | No | 0 |
| `rpm_torque_ratio` | `REAL` | No | `NULL` | No | 0 |
| `rpm_rolling_mean` | `REAL` | No | `NULL` | No | 0 |
| `rpm_rolling_std` | `REAL` | No | `NULL` | No | 0 |
| `torque_rolling_mean` | `REAL` | No | `NULL` | No | 0 |
| `torque_rolling_std` | `REAL` | No | `NULL` | No | 0 |
| `tool_wear_rolling_mean` | `REAL` | No | `NULL` | No | 0 |
| `rpm_toolwear` | `INTEGER` | No | `NULL` | No | 0 |
| `temp_torque` | `REAL` | No | `NULL` | No | 0 |
| `power_toolwear` | `REAL` | No | `NULL` | No | 0 |
| `rpm_change` | `REAL` | No | `NULL` | No | 0 |
| `torque_change` | `REAL` | No | `NULL` | No | 0 |
| `wear_change` | `REAL` | No | `NULL` | No | 0 |
| `wear_remaining` | `INTEGER` | No | `NULL` | No | 0 |
| `hdf_risk` | `INTEGER` | No | `NULL` | No | 0 |
| `pwf_risk` | `INTEGER` | No | `NULL` | No | 0 |
| `osf_risk` | `INTEGER` | No | `NULL` | No | 0 |
| `Type` | `INTEGER` | No | `NULL` | No | 0 |
| `actual_failure` | `INTEGER` | No | `NULL` | No | 0 |
| `pred_prob` | `REAL` | No | `NULL` | No | 0 |
| `risk_score` | `INTEGER` | No | `NULL` | No | 0 |
| `risk_level` | `TEXT` | No | `NULL` | No | 0 |
| `action` | `TEXT` | No | `NULL` | No | 0 |
| `reasons` | `TEXT` | No | `NULL` | No | 0 |
| `override_applied` | `INTEGER` | No | `NULL` | No | 0 |
| `timestamp` | `TEXT` | No | `NULL` | No | 0 |
| `shap_explanation` | `TEXT` | No | `NULL` | No | 0 |
| `isoforest_anomaly` | `INTEGER` | No | `NULL` | No | 0 |
| `zscore_anomaly` | `INTEGER` | No | `NULL` | No | 0 |
| `alert_status` | `TEXT` | No | `NULL` | No | 0 |
| `record_id` | `INTEGER` | No | `NULL` | No | 0 |
| `assigned_to` | `TEXT` | No | `NULL` | No | 0 |
| `resolved_by` | `TEXT` | No | `NULL` | No | 0 |
| `resolved_at` | `TEXT` | No | `NULL` | No | 0 |
| `resolution_note` | `TEXT` | No | `NULL` | No | 0 |

### Foreign Keys

None declared.

### Indexes

| Index Name | Unique | Origin | Partial |
|---|---|---|---|
| `idx_decision_log_risk_level` | No | c | No |

Indexed columns: `risk_level`
| `idx_decision_log_alert_status` | No | c | No |

Indexed columns: `alert_status`
| `idx_decision_log_risk_score` | No | c | No |

Indexed columns: `risk_score`

**Row count:** 2000

---

## `downtime_events`

| Column | Type | Not Null | Default | Primary Key | Primary Key Order |
|---|---|---|---|---|---|
| `downtime_id` | `INTEGER` | No | `NULL` | Yes | 1 |
| `asset_id` | `TEXT` | Yes | `NULL` | No | 0 |
| `machine_id` | `TEXT` | Yes | `NULL` | No | 0 |
| `start_time` | `TEXT` | Yes | `NULL` | No | 0 |
| `end_time` | `TEXT` | No | `NULL` | No | 0 |
| `duration_minutes` | `INTEGER` | No | `NULL` | No | 0 |
| `reason_category` | `TEXT` | Yes | `NULL` | No | 0 |
| `reason_detail` | `TEXT` | No | `NULL` | No | 0 |
| `linked_wo_id` | `INTEGER` | No | `NULL` | No | 0 |
| `reported_by` | `TEXT` | Yes | `NULL` | No | 0 |
| `created_at` | `TEXT` | Yes | `NULL` | No | 0 |

### Foreign Keys

| Column | References Table | References Column | On Update | On Delete |
|---|---|---|---|---|
| `asset_id` | `assets` | `asset_id` | NO ACTION | NO ACTION |

### Indexes

| Index Name | Unique | Origin | Partial |
|---|---|---|---|
| `idx_downtime_events_open` | No | c | No |

Indexed columns: `machine_id`, `end_time`

**Row count:** 7

---

## `health_log`

| Column | Type | Not Null | Default | Primary Key | Primary Key Order |
|---|---|---|---|---|---|
| `id` | `INTEGER` | No | `NULL` | Yes | 1 |
| `timestamp` | `TEXT` | Yes | `NULL` | No | 0 |
| `overall_status` | `TEXT` | Yes | `NULL` | No | 0 |
| `api_latency_ms` | `REAL` | No | `NULL` | No | 0 |
| `db_latency_ms` | `REAL` | No | `NULL` | No | 0 |

### Foreign Keys

None declared.

### Indexes

None declared.

**Row count:** 500

---

## `machine_data`

| Column | Type | Not Null | Default | Primary Key | Primary Key Order |
|---|---|---|---|---|---|
| `Type` | `INTEGER` | No | `NULL` | No | 0 |
| `air_temp` | `REAL` | No | `NULL` | No | 0 |
| `process_temp` | `REAL` | No | `NULL` | No | 0 |
| `rpm` | `INTEGER` | No | `NULL` | No | 0 |
| `torque` | `REAL` | No | `NULL` | No | 0 |
| `tool_wear` | `INTEGER` | No | `NULL` | No | 0 |
| `failure` | `INTEGER` | No | `NULL` | No | 0 |
| `TWF` | `INTEGER` | No | `NULL` | No | 0 |
| `HDF` | `INTEGER` | No | `NULL` | No | 0 |
| `PWF` | `INTEGER` | No | `NULL` | No | 0 |
| `OSF` | `INTEGER` | No | `NULL` | No | 0 |
| `RNF` | `INTEGER` | No | `NULL` | No | 0 |
| `temp_diff` | `REAL` | No | `NULL` | No | 0 |
| `power` | `REAL` | No | `NULL` | No | 0 |
| `tool_torque` | `REAL` | No | `NULL` | No | 0 |
| `tool_wear_index` | `REAL` | No | `NULL` | No | 0 |
| `rpm_torque_ratio` | `REAL` | No | `NULL` | No | 0 |
| `rpm_rolling_mean` | `REAL` | No | `NULL` | No | 0 |
| `rpm_rolling_std` | `REAL` | No | `NULL` | No | 0 |
| `torque_rolling_mean` | `REAL` | No | `NULL` | No | 0 |
| `torque_rolling_std` | `REAL` | No | `NULL` | No | 0 |
| `tool_wear_rolling_mean` | `REAL` | No | `NULL` | No | 0 |
| `hdf_risk` | `INTEGER` | No | `NULL` | No | 0 |
| `pwf_risk` | `INTEGER` | No | `NULL` | No | 0 |
| `osf_risk` | `INTEGER` | No | `NULL` | No | 0 |
| `rpm_toolwear` | `INTEGER` | No | `NULL` | No | 0 |
| `temp_torque` | `REAL` | No | `NULL` | No | 0 |
| `power_toolwear` | `REAL` | No | `NULL` | No | 0 |
| `rpm_change` | `REAL` | No | `NULL` | No | 0 |
| `torque_change` | `REAL` | No | `NULL` | No | 0 |
| `wear_change` | `REAL` | No | `NULL` | No | 0 |
| `wear_remaining` | `INTEGER` | No | `NULL` | No | 0 |
| `zscore_anomaly` | `INTEGER` | No | `NULL` | No | 0 |
| `isoforest_anomaly` | `INTEGER` | No | `NULL` | No | 0 |
| `record_id` | `INTEGER` | No | `NULL` | No | 0 |

### Foreign Keys

None declared.

### Indexes

None declared.

**Row count:** 10000

---

## `maintenance_records`

| Column | Type | Not Null | Default | Primary Key | Primary Key Order |
|---|---|---|---|---|---|
| `maint_id` | `INTEGER` | No | `NULL` | Yes | 1 |
| `asset_id` | `TEXT` | Yes | `NULL` | No | 0 |
| `machine_id` | `TEXT` | Yes | `NULL` | No | 0 |
| `maintenance_type` | `TEXT` | Yes | `NULL` | No | 0 |
| `source_wo_id` | `INTEGER` | No | `NULL` | No | 0 |
| `source_pm_id` | `INTEGER` | No | `NULL` | No | 0 |
| `description` | `TEXT` | Yes | `NULL` | No | 0 |
| `performed_by` | `TEXT` | Yes | `NULL` | No | 0 |
| `performed_at` | `TEXT` | Yes | `NULL` | No | 0 |
| `downtime_minutes` | `INTEGER` | No | `NULL` | No | 0 |
| `created_at` | `TEXT` | Yes | `NULL` | No | 0 |

### Foreign Keys

| Column | References Table | References Column | On Update | On Delete |
|---|---|---|---|---|
| `source_pm_id` | `pm_schedules` | `pm_id` | NO ACTION | NO ACTION |
| `source_wo_id` | `work_orders` | `wo_id` | NO ACTION | NO ACTION |
| `asset_id` | `assets` | `asset_id` | NO ACTION | NO ACTION |

### Indexes

| Index Name | Unique | Origin | Partial |
|---|---|---|---|
| `idx_maintenance_records_machine_id` | No | c | No |

Indexed columns: `machine_id`

**Row count:** 24

---

## `notifications`

| Column | Type | Not Null | Default | Primary Key | Primary Key Order |
|---|---|---|---|---|---|
| `id` | `INTEGER` | No | `NULL` | Yes | 1 |
| `username` | `TEXT` | No | `NULL` | No | 0 |
| `title` | `TEXT` | Yes | `NULL` | No | 0 |
| `message` | `TEXT` | Yes | `NULL` | No | 0 |
| `level` | `TEXT` | Yes | `'info'` | No | 0 |
| `record_id` | `INTEGER` | No | `NULL` | No | 0 |
| `created_at` | `TEXT` | Yes | `NULL` | No | 0 |
| `read_at` | `TEXT` | No | `NULL` | No | 0 |
| `type` | `TEXT` | Yes | `'system'` | No | 0 |

### Foreign Keys

None declared.

### Indexes

| Index Name | Unique | Origin | Partial |
|---|---|---|---|
| `idx_notifications_username` | No | c | No |

Indexed columns: `username`

**Row count:** 85

---

## `pm_schedules`

| Column | Type | Not Null | Default | Primary Key | Primary Key Order |
|---|---|---|---|---|---|
| `pm_id` | `INTEGER` | No | `NULL` | Yes | 1 |
| `asset_id` | `TEXT` | Yes | `NULL` | No | 0 |
| `machine_id` | `TEXT` | Yes | `NULL` | No | 0 |
| `pm_name` | `TEXT` | Yes | `NULL` | No | 0 |
| `frequency_type` | `TEXT` | Yes | `NULL` | No | 0 |
| `frequency_value` | `INTEGER` | Yes | `NULL` | No | 0 |
| `last_performed_at` | `TEXT` | No | `NULL` | No | 0 |
| `last_performed_tool_wear` | `INTEGER` | No | `NULL` | No | 0 |
| `next_due_at` | `TEXT` | No | `NULL` | No | 0 |
| `status` | `TEXT` | Yes | `'ON_SCHEDULE'` | No | 0 |
| `created_by` | `TEXT` | Yes | `NULL` | No | 0 |
| `created_at` | `TEXT` | Yes | `NULL` | No | 0 |

### Foreign Keys

| Column | References Table | References Column | On Update | On Delete |
|---|---|---|---|---|
| `asset_id` | `assets` | `asset_id` | NO ACTION | NO ACTION |

### Indexes

| Index Name | Unique | Origin | Partial |
|---|---|---|---|
| `idx_pm_schedules_machine_id` | No | c | No |

Indexed columns: `machine_id`

**Row count:** 24

---

## `predictions`

| Column | Type | Not Null | Default | Primary Key | Primary Key Order |
|---|---|---|---|---|---|
| `air_temp` | `REAL` | No | `NULL` | No | 0 |
| `process_temp` | `REAL` | No | `NULL` | No | 0 |
| `rpm` | `INTEGER` | No | `NULL` | No | 0 |
| `torque` | `REAL` | No | `NULL` | No | 0 |
| `tool_wear` | `INTEGER` | No | `NULL` | No | 0 |
| `temp_diff` | `REAL` | No | `NULL` | No | 0 |
| `power` | `REAL` | No | `NULL` | No | 0 |
| `tool_torque` | `REAL` | No | `NULL` | No | 0 |
| `tool_wear_index` | `REAL` | No | `NULL` | No | 0 |
| `rpm_torque_ratio` | `REAL` | No | `NULL` | No | 0 |
| `rpm_rolling_mean` | `REAL` | No | `NULL` | No | 0 |
| `rpm_rolling_std` | `REAL` | No | `NULL` | No | 0 |
| `torque_rolling_mean` | `REAL` | No | `NULL` | No | 0 |
| `torque_rolling_std` | `REAL` | No | `NULL` | No | 0 |
| `tool_wear_rolling_mean` | `REAL` | No | `NULL` | No | 0 |
| `rpm_toolwear` | `INTEGER` | No | `NULL` | No | 0 |
| `temp_torque` | `REAL` | No | `NULL` | No | 0 |
| `power_toolwear` | `REAL` | No | `NULL` | No | 0 |
| `rpm_change` | `REAL` | No | `NULL` | No | 0 |
| `torque_change` | `REAL` | No | `NULL` | No | 0 |
| `wear_change` | `REAL` | No | `NULL` | No | 0 |
| `wear_remaining` | `INTEGER` | No | `NULL` | No | 0 |
| `hdf_risk` | `INTEGER` | No | `NULL` | No | 0 |
| `pwf_risk` | `INTEGER` | No | `NULL` | No | 0 |
| `osf_risk` | `INTEGER` | No | `NULL` | No | 0 |
| `Type` | `INTEGER` | No | `NULL` | No | 0 |
| `actual_failure` | `INTEGER` | No | `NULL` | No | 0 |
| `pred_prob_lr` | `REAL` | No | `NULL` | No | 0 |
| `pred_prob_rf` | `REAL` | No | `NULL` | No | 0 |
| `pred_label_rf` | `INTEGER` | No | `NULL` | No | 0 |
| `model_agreement` | `INTEGER` | No | `NULL` | No | 0 |
| `risk_level` | `TEXT` | No | `NULL` | No | 0 |
| `record_id` | `INTEGER` | No | `NULL` | No | 0 |

### Foreign Keys

None declared.

### Indexes

None declared.

**Row count:** 2000

---

## `production_lines`

| Column | Type | Not Null | Default | Primary Key | Primary Key Order |
|---|---|---|---|---|---|
| `line_id` | `TEXT` | No | `NULL` | Yes | 1 |
| `line_name` | `TEXT` | Yes | `NULL` | No | 0 |
| `location` | `TEXT` | No | `NULL` | No | 0 |
| `status` | `TEXT` | Yes | `'RUNNING'` | No | 0 |

### Foreign Keys

None declared.

### Indexes

| Index Name | Unique | Origin | Partial |
|---|---|---|---|
| `sqlite_autoindex_production_lines_1` | Yes | pk | No |

Indexed columns: `line_id`

**Row count:** 4

---

## `shap_values`

| Column | Type | Not Null | Default | Primary Key | Primary Key Order |
|---|---|---|---|---|---|
| `air_temp` | `REAL` | No | `NULL` | No | 0 |
| `process_temp` | `REAL` | No | `NULL` | No | 0 |
| `rpm` | `REAL` | No | `NULL` | No | 0 |
| `torque` | `REAL` | No | `NULL` | No | 0 |
| `tool_wear` | `REAL` | No | `NULL` | No | 0 |
| `temp_diff` | `REAL` | No | `NULL` | No | 0 |
| `power` | `REAL` | No | `NULL` | No | 0 |
| `tool_torque` | `REAL` | No | `NULL` | No | 0 |
| `tool_wear_index` | `REAL` | No | `NULL` | No | 0 |
| `rpm_torque_ratio` | `REAL` | No | `NULL` | No | 0 |
| `rpm_rolling_mean` | `REAL` | No | `NULL` | No | 0 |
| `rpm_rolling_std` | `REAL` | No | `NULL` | No | 0 |
| `torque_rolling_mean` | `REAL` | No | `NULL` | No | 0 |
| `torque_rolling_std` | `REAL` | No | `NULL` | No | 0 |
| `tool_wear_rolling_mean` | `REAL` | No | `NULL` | No | 0 |
| `rpm_toolwear` | `REAL` | No | `NULL` | No | 0 |
| `temp_torque` | `REAL` | No | `NULL` | No | 0 |
| `power_toolwear` | `REAL` | No | `NULL` | No | 0 |
| `rpm_change` | `REAL` | No | `NULL` | No | 0 |
| `torque_change` | `REAL` | No | `NULL` | No | 0 |
| `wear_change` | `REAL` | No | `NULL` | No | 0 |
| `wear_remaining` | `REAL` | No | `NULL` | No | 0 |
| `hdf_risk` | `REAL` | No | `NULL` | No | 0 |
| `pwf_risk` | `REAL` | No | `NULL` | No | 0 |
| `osf_risk` | `REAL` | No | `NULL` | No | 0 |
| `Type` | `REAL` | No | `NULL` | No | 0 |
| `record_id` | `INTEGER` | No | `NULL` | No | 0 |
| `top_feature` | `TEXT` | No | `NULL` | No | 0 |
| `top_shap_value` | `REAL` | No | `NULL` | No | 0 |

### Foreign Keys

None declared.

### Indexes

None declared.

**Row count:** 2000

---

## `users`

| Column | Type | Not Null | Default | Primary Key | Primary Key Order |
|---|---|---|---|---|---|
| `id` | `INTEGER` | No | `NULL` | Yes | 1 |
| `username` | `TEXT` | Yes | `NULL` | No | 0 |
| `email` | `TEXT` | Yes | `NULL` | No | 0 |
| `password_hash` | `TEXT` | Yes | `NULL` | No | 0 |
| `role` | `TEXT` | Yes | `'technician'` | No | 0 |
| `is_active` | `INTEGER` | Yes | `1` | No | 0 |
| `created_at` | `TEXT` | Yes | `NULL` | No | 0 |
| `last_login` | `TEXT` | No | `NULL` | No | 0 |
| `must_change_password` | `INTEGER` | Yes | `0` | No | 0 |

### Foreign Keys

None declared.

### Indexes

| Index Name | Unique | Origin | Partial |
|---|---|---|---|
| `sqlite_autoindex_users_2` | Yes | u | No |

Indexed columns: `email`
| `sqlite_autoindex_users_1` | Yes | u | No |

Indexed columns: `username`

**Row count:** 4

---

## `work_orders`

| Column | Type | Not Null | Default | Primary Key | Primary Key Order |
|---|---|---|---|---|---|
| `wo_id` | `INTEGER` | No | `NULL` | Yes | 1 |
| `record_id` | `INTEGER` | No | `NULL` | No | 0 |
| `machine_id` | `TEXT` | No | `NULL` | No | 0 |
| `asset_id` | `TEXT` | No | `NULL` | No | 0 |
| `title` | `TEXT` | Yes | `NULL` | No | 0 |
| `description` | `TEXT` | No | `NULL` | No | 0 |
| `recommended_action` | `TEXT` | No | `NULL` | No | 0 |
| `risk_level` | `TEXT` | No | `NULL` | No | 0 |
| `risk_score` | `INTEGER` | No | `NULL` | No | 0 |
| `priority` | `TEXT` | Yes | `'MEDIUM'` | No | 0 |
| `status` | `TEXT` | Yes | `'OPEN'` | No | 0 |
| `assigned_to` | `TEXT` | No | `NULL` | No | 0 |
| `created_by` | `TEXT` | Yes | `NULL` | No | 0 |
| `created_at` | `TEXT` | Yes | `NULL` | No | 0 |
| `due_date` | `TEXT` | No | `NULL` | No | 0 |
| `completed_at` | `TEXT` | No | `NULL` | No | 0 |
| `completion_note` | `TEXT` | No | `NULL` | No | 0 |
| `accepted_at` | `TEXT` | No | `NULL` | No | 0 |
| `started_at` | `TEXT` | No | `NULL` | No | 0 |
| `technician_notes` | `TEXT` | No | `NULL` | No | 0 |
| `resolution_note` | `TEXT` | No | `NULL` | No | 0 |
| `verification_note` | `TEXT` | No | `NULL` | No | 0 |
| `verified_by` | `TEXT` | No | `NULL` | No | 0 |
| `verified_at` | `TEXT` | No | `NULL` | No | 0 |

### Foreign Keys

| Column | References Table | References Column | On Update | On Delete |
|---|---|---|---|---|
| `record_id` | `decision_log` | `record_id` | NO ACTION | NO ACTION |

### Indexes

| Index Name | Unique | Origin | Partial |
|---|---|---|---|
| `idx_work_orders_status` | No | c | No |

Indexed columns: `status`
| `idx_work_orders_machine_id` | No | c | No |

Indexed columns: `machine_id`

**Row count:** 9

---

