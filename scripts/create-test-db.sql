-- Runs once when the local development database container is first created.
-- A separate, disposable database for the test suite (tests drop and recreate its tables).
CREATE DATABASE agent_gateway_test OWNER gateway;
