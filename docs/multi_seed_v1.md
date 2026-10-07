# Out-of-sample seeds (v1)

Days = days from incident start to first matching alert.

| seed | INC-01 | INC-02 | INC-03 | INC-04 | INC-05 | false_alarms | secondary | model | roc_auc | lift_top10 | churns_flagged |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 42 | 29d | 5d | 394d | 3d | missed | 10 | 2 | logistic_regression | 0.731 | 2.95 | 44% |
| 7 | 29d | 5d | missed | 3d | 244d | 10 | 3 | logistic_regression | 0.79 | 3.78 | 58% |
| 2024 | 29d | 5d | 303d | 3d | 214d | 14 | 2 | logistic_regression | 0.727 | 2.72 | 55% |
| 31337 | 29d | 5d | missed | 3d | 244d | 13 | 3 | logistic_regression | 0.757 | 3.2 | 52% |
