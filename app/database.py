import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# Defaults to a local SQLite file for zero-friction v1 testing.
# Swap to Postgres later by setting DATABASE_URL, e.g.:
#   postgresql+psycopg2://user:pass@localhost:5432/tigarsec_assess
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/tigarsec_assess.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
