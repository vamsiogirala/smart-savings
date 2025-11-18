from sqlalchemy import create_engine, Column, Integer, String, Float
from sqlalchemy.orm import sessionmaker, declarative_base

# SQLite database file in the project folder
DATABASE_URL = "sqlite:///./smart_savings.db"

engine = create_engine(
    DATABASE_URL, connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    date = Column(String(10), index=True)        # "YYYY-MM-DD"
    category = Column(String(50), index=True)
    description = Column(String(255))
    amount = Column(Float)


def init_db():
    """Create tables if they don't exist."""
    Base.metadata.create_all(bind=engine)
