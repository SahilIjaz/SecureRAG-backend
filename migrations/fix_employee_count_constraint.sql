-- Fix the employee_count_range constraint to match frontend options
-- This drops the old constraint and adds the new one

ALTER TABLE tenants DROP CONSTRAINT tenants_employee_count_range_check;

ALTER TABLE tenants ADD CONSTRAINT tenants_employee_count_range_check
    CHECK (
        employee_count_range IS NULL OR
        employee_count_range IN ('1-15', '16-49', '50-199', '200-1999', '2000-4999', 'just-me')
    );
