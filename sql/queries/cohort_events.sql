-- Raw cohort history for statistics. Bind :as_of, :cohort_from, :cohort_to.
-- Same cohort rule as snapshot.sql: submitted_on within the cohort and <= as_of,
-- and only events with occurred_on <= as_of.
WITH params AS (
    SELECT coalesce(:as_of, date('now', 'localtime')) AS as_of,
           :cohort_from AS cohort_from, :cohort_to AS cohort_to
), cohort AS (
    SELECT a.id, a.submitted_on FROM applications a CROSS JOIN params p
    WHERE a.submitted_on <= p.as_of
      AND (p.cohort_from IS NULL OR a.submitted_on >= p.cohort_from)
      AND (p.cohort_to IS NULL OR a.submitted_on <= p.cohort_to)
)
SELECT c.id AS application_id, c.submitted_on, e.occurred_on, e.day_order,
       e.event_type, t.application_state, e.round_id, r.phase AS round_phase
FROM cohort c
LEFT JOIN events e ON e.application_id = c.id
     AND e.occurred_on <= (SELECT as_of FROM params)
LEFT JOIN event_types t ON t.code = e.event_type
LEFT JOIN rounds r ON r.id = e.round_id
ORDER BY c.id, e.occurred_on, e.day_order;
