# Offline TG-detector validation

- total tasks: 20
- failed: 13  |  passed: 7

## H5 — coverage on failed trajectories
- fraction of failures with ≥1 detector fire (any turn): **69.2%** (9/13)
- fraction firing **before** the final turn (actionable online): **61.5%** (8/13)
- **H5 decision gate (≥60% pre-final): PASS**

## First-fire turn index on failures
- absolute turn idx: min=3  med=5.0  max=15
- relative position in trajectory: min=0.27  med=0.60  max=1.00

## False-positive rate on successful trajectories
- fraction with any detector firing: **57.1%** (4/7)
- (ideal: low. High FPR means recoveries would disrupt successful runs.)

## Detector fire counts (all tasks)
| Detector | fires |
|---|---|
| D1 | 31 |
| D2 | 5 |
| D3 | 0 |

## First-firing detector on failures (one per failed task)
- D1: 8
- (none): 4
- D2: 1

## Per-task replay
| task | succ | turns | first fire (turn / id) | total fires |
|---|---|---|---|---|
| 0 | ✗ | 12 | — | 0 |
| 1 | ✓ | 10 | — | 0 |
| 2 | ✓ | 21 | — | 0 |
| 3 | ✗ | 7 | 6 / D2 | 1 |
| 4 | ✗ | 12 | 3 / D1 | 5 |
| 5 | ✓ | 14 | 11 / D1 | 2 |
| 6 | ✗ | 17 | 5 / D1 | 2 |
| 7 | ✓ | 16 | 5 / D1 | 1 |
| 8 | ✗ | 21 | 15 / D1 | 3 |
| 9 | ✓ | 14 | — | 0 |
| 10 | ✓ | 6 | 5 / D2 | 1 |
| 11 | ✗ | 9 | 3 / D1 | 7 |
| 12 | ✗ | 13 | — | 0 |
| 13 | ✗ | 19 | 8 / D1 | 2 |
| 14 | ✗ | 13 | — | 0 |
| 15 | ✗ | 5 | 3 / D1 | 3 |
| 16 | ✗ | 6 | 3 / D1 | 4 |
| 17 | ✓ | 11 | 5 / D1 | 1 |
| 18 | ✗ | 9 | 5 / D1 | 4 |
| 19 | ✗ | 16 | — | 0 |