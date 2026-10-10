-- Current-state convenience views. For historical snapshots filter events BEFORE ranking.
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

CREATE VIEW v_application_timeline AS
SELECT a.id AS application_id, NULL AS event_id, NULL AS round_id,
       a.submitted_on AS occurred_on, 0 AS day_order, 'submitted' AS event_type,
       'Application submitted' AS label, NULL AS scheduled_for,
       NULL AS scheduled_time, NULL AS timezone, NULL AS notes
FROM applications a
UNION ALL
SELECT e.application_id, e.id, e.round_id, e.occurred_on, e.day_order,
       e.event_type, t.label, e.scheduled_for, e.scheduled_time, e.timezone, e.notes
FROM events e JOIN event_types t ON t.code = e.event_type;

CREATE VIEW v_application_status AS
WITH ranked_states AS (
    SELECT e.application_id, t.application_state, e.occurred_on,
           row_number() OVER (
               PARTITION BY e.application_id ORDER BY e.occurred_on DESC, e.day_order DESC
           ) AS rn
    FROM events e JOIN event_types t ON t.code = e.event_type
    WHERE t.application_state IS NOT NULL
), activity AS (
    SELECT application_id, max(occurred_on) AS last_activity_on FROM events GROUP BY application_id
), completions AS (
    SELECT e.application_id,
           sum(CASE WHEN r.phase = 'interview' THEN 1 ELSE 0 END) AS completed_interviews,
           sum(CASE WHEN r.phase = 'screening' THEN 1 ELSE 0 END) AS completed_screens,
           sum(CASE WHEN r.phase = 'assessment' THEN 1 ELSE 0 END) AS completed_assessments
    FROM events e JOIN rounds r ON r.id = e.round_id
    WHERE e.event_type IN ('completed', 'assessment_submitted')
    GROUP BY e.application_id
)
SELECT a.*, coalesce(s.application_state, 'in_progress') AS current_status,
       coalesce(s.occurred_on, a.submitted_on) AS status_since,
       coalesce(x.last_activity_on, a.submitted_on) AS last_activity_on,
       coalesce(c.completed_interviews, 0) AS completed_interviews,
       coalesce(c.completed_screens, 0) AS completed_screens,
       coalesce(c.completed_assessments, 0) AS completed_assessments
FROM applications a
LEFT JOIN ranked_states s ON s.application_id = a.id AND s.rn = 1
LEFT JOIN activity x ON x.application_id = a.id
LEFT JOIN completions c ON c.application_id = a.id;

CREATE VIEW v_round_status AS
WITH ranked_states AS (
    SELECT e.round_id, t.round_state, e.occurred_on,
           row_number() OVER (PARTITION BY e.round_id ORDER BY e.occurred_on DESC, e.day_order DESC) AS rn
    FROM events e JOIN event_types t ON t.code = e.event_type
    WHERE e.round_id IS NOT NULL AND t.round_state IS NOT NULL
), ranked_schedule AS (
    SELECT round_id, scheduled_for, scheduled_time, timezone,
           row_number() OVER (PARTITION BY round_id ORDER BY occurred_on DESC, day_order DESC) AS rn
    FROM events WHERE event_type IN ('scheduled', 'rescheduled')
)
SELECT r.*, coalesce(s.round_state, 'planned') AS current_status,
       s.occurred_on AS status_since, p.scheduled_for, p.scheduled_time, p.timezone
FROM rounds r
LEFT JOIN ranked_states s ON s.round_id = r.id AND s.rn = 1
LEFT JOIN ranked_schedule p ON p.round_id = r.id AND p.rn = 1;

INSERT INTO schema_migrations(version, name) VALUES (2, 'reporting_views');
PRAGMA user_version = 2;
COMMIT;
