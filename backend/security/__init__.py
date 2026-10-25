"""SiliconBreak X: hardware-security investigation, repair and verification.

An additive extension of SiliconBreak X. It reuses SiliconBreak X's runs, event bus,
restricted subprocess runner, Verilator and Vivado adapters and approval gates,
and adds:

  cases.py        trusted case manifests (contract, tests, formal wrapper, hidden reference)
  verify.py       the trusted verifier: owns every pass/fail decision
  formal.py       Yosys SAT (bounded check + temporal induction) property checks
  hostsafety.py   rejects RTL that could reach the host before any tool compiles it
  investigate.py  model or rule-based investigation -> findings (always "suspected")
  repair.py       repair attempts (Factory Droid, model patch, human reference) in
                  isolated copies; never touch trusted tests
  orchestrator.py the audit pipeline (a SiliconBreak X run with security stages)
  bench.py        SiliconBench: case runs, baselines and stored metrics

Finding status ladder (never skipped):
  suspected -> reproduced -> patch_proposed -> repaired_verified
  (or repair_failed / false_positive / inconclusive)
"""
