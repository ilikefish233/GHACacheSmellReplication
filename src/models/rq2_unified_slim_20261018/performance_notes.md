
# RQ2 grouping performance

The original experimental pipeline took about 24,112 seconds, including
detection, RQ1, and RQ2. File timestamps suggest roughly 18,000 seconds
for the RQ2 stage. These timings are approximate.

The original design repeated regression and bootstrap work for each smell.
The unified model fits one design per outcome and groups runs with identical
predictors. It retains group counts, success counts, and the first two
moments of log duration, preserving fitted coefficients and cluster scores
within numerical precision.

A cold run including YAML feature extraction took 250.1 seconds. The cached
run took 111.3 seconds. Input hashing took 7.3 seconds, feature cache loading
12.1 seconds, and reading and aggregating the 4.2 GB run input 66.6 seconds.
The completed and successful duration endpoints took 5.0 and 3.9 seconds.
The success endpoint including Firth sensitivity took 16.3 seconds.

In a separate synthetic benchmark with 100,000 runs, 4,000 groups and 12
columns, one logistic fit took 0.132 seconds on expanded rows and 0.0096
seconds on grouped rows. The largest coefficient difference was 1.7e-17.
See the benchmark JSON for the original measurements.
