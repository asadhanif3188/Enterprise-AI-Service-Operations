
-- Development seed data
-- All organizations and cases are fictional.

BEGIN;

-- Create two customer organizations.
INSERT INTO organization (name, slug)
VALUES
    ('Northwind Services', 'northwind'),
    ('Summit Technologies', 'summit')
ON CONFLICT (slug) DO NOTHING;

-- Create a case for Northwind Services.
INSERT INTO case_record (
    organization_id,
    title,
    description
)
SELECT
    id,
    'Employees cannot connect to VPN',
    'Multiple users report VPN connection failures.'
FROM organization
WHERE slug = 'northwind'
AND NOT EXISTS (
    SELECT 1
    FROM case_record
    WHERE organization_id = organization.id
      AND title = 'Employees cannot connect to VPN'
);

-- Create a case for Summit Technologies.
INSERT INTO case_record (
    organization_id,
    title,
    description
)
SELECT
    id,
    'Application responds slowly',
    'Users report slow application response times.'
FROM organization
WHERE slug = 'summit'
AND NOT EXISTS (
    SELECT 1
    FROM case_record
    WHERE organization_id = organization.id
      AND title = 'Application responds slowly'
);

COMMIT;
