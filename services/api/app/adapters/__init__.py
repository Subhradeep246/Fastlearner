"""Adapters implement ports declared by the service and domain layers.

Adapters translate external systems (files, malware scanners, AI providers,
graph stores, and queues) into the vendor-neutral contracts the domain depends
on. Domain and service code never imports an adapter implementation directly.
"""
