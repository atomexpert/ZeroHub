# ZeroHub project passport

ZeroHub is a self-hosted FastAPI and PWA workspace for AI agents on Windows and Linux.
Use run_server.py or the platform launcher. Local state lives in data/, users/ and shared_projects/.
Keep tests isolated from the running service and its SQLite database. Run tools/verify_hub.py
before committing, retain the existing access controls, and run speech recognition on the CPU.
