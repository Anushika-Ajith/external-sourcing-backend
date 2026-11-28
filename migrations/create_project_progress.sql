-- Project / works MIS table for WhatsApp "analyse project progress" flow.
CREATE TABLE IF NOT EXISTS project_progress (
    project_progress_id      BIGSERIAL PRIMARY KEY,

    sl_no                    INTEGER,
    project_code             VARCHAR(100) NOT NULL,
    work_type                VARCHAR(100),
    project_status           VARCHAR(100),
    district_name            VARCHAR(150),
    block_name               VARCHAR(150),
    name_of_work             TEXT NOT NULL,
    client                   VARCHAR(255),
    project_type             VARCHAR(100),
    value_cr                 NUMERIC(14, 4),

    project_start_date       DATE,
    scheduled_duration       VARCHAR(50),
    scheduled_completion_date DATE,
    project_months_over      NUMERIC(8, 2),
    days_left_scheduled      INTEGER,

    planned_physical_progress NUMERIC(8, 2),
    actual_physical_progress  NUMERIC(8, 2),
    planned_vs_actual_progress NUMERIC(8, 2),
    actual_completion_date   DATE,
    delay_category           VARCHAR(100),

    tenant_id                INTEGER NOT NULL DEFAULT 1,
    company_id               INTEGER NOT NULL DEFAULT 3,

    created_by               INTEGER,
    created_on               TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
    created_ip               VARCHAR(45),
    modified_by              INTEGER,
    modified_on              TIMESTAMP WITHOUT TIME ZONE,
    modified_ip              VARCHAR(45)
);

CREATE INDEX IF NOT EXISTS idx_project_progress_code
    ON project_progress (project_code);

CREATE INDEX IF NOT EXISTS idx_project_progress_status
    ON project_progress (project_status);

CREATE INDEX IF NOT EXISTS idx_project_progress_district
    ON project_progress (district_name);

CREATE INDEX IF NOT EXISTS idx_project_progress_delay
    ON project_progress (delay_category);

CREATE INDEX IF NOT EXISTS idx_project_progress_tenant_company
    ON project_progress (tenant_id, company_id);

CREATE INDEX IF NOT EXISTS idx_project_progress_dates
    ON project_progress (project_start_date, scheduled_completion_date);
