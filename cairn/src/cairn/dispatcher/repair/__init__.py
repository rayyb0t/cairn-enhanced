"""The repair layer's dispatcher side, Phase 2 (shadow mode): runs are registered and copied, nothing is applied.

Used only for explore runs of projects whose Repair switch is on, and, when dispatch.yaml has a repair:
section, by one housekeeping thread started with the dispatcher. Without that section and with every switch
off, nothing under this package is imported (scheduler/loop.py imports it only on those paths).
"""
