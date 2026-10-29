-- Demo source for Trino federation (TRN-004). Runs once, when the container's data directory is created.
-- `sales` holds customers keyed to TPC-H nations, so a query can join catalog pg to catalog tpch.
CREATE DATABASE trino_demo;
\connect trino_demo
CREATE SCHEMA sales;
COMMENT ON SCHEMA sales IS 'Demo sales data federated with TPC-H through Trino';
CREATE TABLE sales.customer (
    id integer PRIMARY KEY,
    name text NOT NULL,
    nation_key bigint NOT NULL,
    email text
);
COMMENT ON TABLE sales.customer IS 'Buyers, keyed to tpch.tiny.nation';
COMMENT ON COLUMN sales.customer.nation_key IS 'TPC-H nation key';
CREATE TABLE sales.orders (
    id integer PRIMARY KEY,
    customer_id integer NOT NULL REFERENCES sales.customer (id),
    amount numeric(10, 2) NOT NULL,
    placed_at timestamp NOT NULL
);
INSERT INTO sales.customer VALUES
    (1, 'Ada', 7, 'ada@example.invalid'),      -- GERMANY (EUROPE)
    (2, 'Bob', 7, 'bob@example.invalid'),      -- GERMANY (EUROPE)
    (3, 'Chen', 18, 'chen@example.invalid'),   -- CHINA (ASIA)
    (4, 'Dana', 24, 'dana@example.invalid'),   -- UNITED STATES (AMERICA)
    (5, 'Eli', 6, 'eli@example.invalid');      -- FRANCE (EUROPE)
INSERT INTO sales.orders VALUES
    (1, 1, 100.00, '2026-01-05 10:00'), (2, 1, 50.00, '2026-02-01 09:30'),
    (3, 2, 25.50, '2026-02-11 14:00'), (4, 3, 300.00, '2026-03-03 08:15'),
    (5, 4, 80.25, '2026-03-20 17:45'), (6, 5, 19.75, '2026-04-02 12:00');
ANALYZE sales.customer;
ANALYZE sales.orders;
-- Trino's catalog login: SELECT only (defense in depth; AnalystOS also reads in a READ ONLY transaction).
CREATE ROLE trino_ro LOGIN PASSWORD 'trino_ro';
GRANT CONNECT ON DATABASE trino_demo TO trino_ro;
GRANT USAGE ON SCHEMA sales TO trino_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA sales TO trino_ro;
