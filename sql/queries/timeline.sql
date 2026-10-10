-- Bind :application_id. Submission is synthesized from applications.submitted_on.
SELECT * FROM v_application_timeline
WHERE application_id = :application_id
ORDER BY occurred_on, day_order;
