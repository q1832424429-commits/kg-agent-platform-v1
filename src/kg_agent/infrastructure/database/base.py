"""定义 SQLAlchemy 声明式模型公共基类。"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """全部 ORM 表模型共享的声明式基类。"""
