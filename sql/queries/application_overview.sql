-- Browse/search overview. Bind :status (NULL = any) and :search (NULL = any).
-- The search value is used as a LIKE pattern, e.g. '%acme%'.
SELECT v.id, v.company, v.role, v.current_status, v.submitted_on, v.status_since,
       v.last_activity_on, v.completed_interviews, v.completed_screens,
       v.completed_assessments,
       (SELECT min(r.scheduled_for) FROM v_round_status r
        WHERE r.application_id = v.id AND r.current_status IN ('invited', 'scheduled')
          AND r.scheduled_for IS NOT NULL) AS next_appointment
FROM v_application_status v
WHERE (:status IS NULL OR v.current_status = :status)
  AND (:search IS NULL
       OR lower(v.company) LIKE :search
       OR lower(v.role) LIKE :search
       OR lower(v.company || ' ' || v.role) LIKE :search)
ORDER BY v.last_activity_on DESC, v.submitted_on DESC, v.company;
