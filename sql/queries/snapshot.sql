-- Bind :as_of, :cohort_from, :cohort_to as YYYY-MM-DD or NULL.
-- Unbound parameters are NULL in sqlite3. Defaults: all submitted applications through today.
-- This is corrected historical truth, not a frozen record of what was known at the time.
WITH params AS (
    SELECT coalesce(:as_of, date('now', 'localtime')) AS as_of,
           :cohort_from AS cohort_from, :cohort_to AS cohort_to
), cohort AS (
    SELECT a.* FROM applications a CROSS JOIN params p
    WHERE a.submitted_on <= p.as_of
      AND (p.cohort_from IS NULL OR a.submitted_on >= p.cohort_from)
      AND (p.cohort_to IS NULL OR a.submitted_on <= p.cohort_to)
), eligible_events AS (
    SELECT e.* FROM events e JOIN cohort a ON a.id = e.application_id CROSS JOIN params p
    WHERE e.occurred_on <= p.as_of
), ranked_states AS (
    SELECT e.application_id, t.application_state,
           row_number() OVER (PARTITION BY e.application_id ORDER BY e.occurred_on DESC, e.day_order DESC) AS rn
    FROM eligible_events e JOIN event_types t ON t.code = e.event_type
    WHERE t.application_state IS NOT NULL
), completions AS (
    SELECT e.application_id,
           sum(CASE WHEN r.phase = 'interview' THEN 1 ELSE 0 END) AS completed_interviews,
           sum(CASE WHEN r.phase = 'screening' THEN 1 ELSE 0 END) AS completed_screens,
           sum(CASE WHEN r.phase = 'assessment' THEN 1 ELSE 0 END) AS completed_assessments
    FROM eligible_events e JOIN rounds r ON r.id = e.round_id
    WHERE e.event_type IN ('completed', 'assessment_submitted')
    GROUP BY e.application_id
), offers AS (
    SELECT application_id, count(*) AS offer_events FROM eligible_events
    WHERE event_type = 'offer_received' GROUP BY application_id
), last_round_state AS (
    -- The newest round-scoped event at or before as_of. When that event is an
    -- invitation or a booking, the round's phase is the work that is still booked.
    SELECT e.application_id, r.phase, t.round_state,
           row_number() OVER (PARTITION BY e.application_id
                              ORDER BY e.occurred_on DESC, e.day_order DESC) AS rn
    FROM eligible_events e
    JOIN event_types t ON t.code = e.event_type
    JOIN rounds r ON r.id = e.round_id
    WHERE t.round_state IS NOT NULL
)
SELECT a.id, a.company, a.role, a.submitted_on,
       a.discovery_source, a.submission_channel, a.recruiter_type,
       coalesce(s.application_state, 'in_progress') AS status,
       coalesce(c.completed_interviews, 0) AS completed_interviews,
       coalesce(c.completed_screens, 0) AS completed_screens,
       coalesce(c.completed_assessments, 0) AS completed_assessments,
       CASE WHEN o.offer_events > 0 THEN 1 ELSE 0 END AS received_offer,
       CASE WHEN l.round_state IN ('invited', 'scheduled') THEN l.phase END AS pending_phase
FROM cohort a
LEFT JOIN ranked_states s ON s.application_id = a.id AND s.rn = 1
LEFT JOIN completions c ON c.application_id = a.id
LEFT JOIN offers o ON o.application_id = a.id
LEFT JOIN last_round_state l ON l.application_id = a.id AND l.rn = 1
ORDER BY a.submitted_on, a.id;
