"""Dreaming subsystem (M11).

M11.1 (current): filesystem sandbox, memory-correction proposals only.
M11.2 (future):  container sandbox + implementer + code proposals.
M11.3 (future):  hardening + adversarial test suite.

The dream cycle runs nightly under launchd. It snapshots the production DB
into a fresh sandbox, runs the dream agent (Opus, extended thinking) against
the snapshot to look for contradictions and stale facts, and emits structured
proposal artifacts to ~/agents/dream-queue/. Production polls the queue and
surfaces pending proposals in the morning interaction.
"""
