"""Application services: use cases and transaction orchestration.

Services depend inward on the domain and declare repository/adapter ports. They
resolve the effective owner scope server-side and never trust a client-supplied
owner identifier.
"""
