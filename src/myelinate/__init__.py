"""Myelinate: self-improving skills and memory for any AI agent framework, with governance built in."""

from .client import EmbeddedClient, GatewayClient, LearnerClient
from .models import EpisodeIn, FeedbackIn, Outcome, RecallRequest, Scope

__all__ = ["EmbeddedClient", "EpisodeIn", "FeedbackIn", "GatewayClient", "LearnerClient", "Outcome",
           "RecallRequest", "Scope"]
__version__ = "0.1.0"
