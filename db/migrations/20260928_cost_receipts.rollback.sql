-- Evidence-preserving rollback: roll back application code, retain nullable columns.
-- Old readers ignore these fields. Never DROP receipts or rewrite historical costs.
-- Keep the incomplete-total labels from S1f when rolling back producers.
SELECT 1;
