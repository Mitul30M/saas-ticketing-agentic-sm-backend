"""Taxonomy shared by agent outputs, tools, evaluation, and the UI."""

from enum import StrEnum


class Category(StrEnum):
    INVOICE_QUESTION = "invoice_question"
    REFUND_REQUEST = "refund_request"
    PLAN_CHANGE = "plan_change"
    LOGIN_ACCOUNT_ACCESS = "login_account_access"
    API_INTEGRATION = "api_integration"
    SYNC_ISSUES = "sync_issues"
    SERVICE_OUTAGE = "service_outage"
    SECURITY_INCIDENT = "security_incident"
    FEATURE_REQUEST = "feature_request"
    OTHER = "other"


class Priority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class Sentiment(StrEnum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"


class Queue(StrEnum):
    BILLING = "billing"
    ACCOUNT = "account"
    TECHNICAL = "technical"
    SECURITY = "security"
    INCIDENTS = "incidents"
    PRODUCT = "product"
    GENERAL = "general"


CATEGORY_QUEUE: dict[Category, Queue] = {
    Category.INVOICE_QUESTION: Queue.BILLING,
    Category.REFUND_REQUEST: Queue.BILLING,
    Category.PLAN_CHANGE: Queue.BILLING,
    Category.LOGIN_ACCOUNT_ACCESS: Queue.ACCOUNT,
    Category.API_INTEGRATION: Queue.TECHNICAL,
    Category.SYNC_ISSUES: Queue.TECHNICAL,
    Category.SERVICE_OUTAGE: Queue.INCIDENTS,
    Category.SECURITY_INCIDENT: Queue.SECURITY,
    Category.FEATURE_REQUEST: Queue.PRODUCT,
    Category.OTHER: Queue.GENERAL,
}
