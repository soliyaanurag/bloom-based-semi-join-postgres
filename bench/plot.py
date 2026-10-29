"""
plot.py: draws the mid-sem charts as PNG files in results/.

From results/results.csv (written by run_experiments.py), one chart per
width variant (narrow, wide) for each of:
  total_bytes_vs_n_<width>.png   estimated bytes on the wire vs n (log scale)
  time_ms_vs_n_<width>.png       query time vs n
  remote_rows_vs_n_<width>.png   rows site B sends back vs n
From results/b_sweep.csv (written by b_sweep.py), per width variant:
  total_bytes_vs_b_<width>.png   network bytes vs b, with a dashed line at
                                 the b* predicted by sjb_best_bits
And one chart that does not depend on the width:
  fp_rate.png                    measured vs predicted false-positive rate.
                                 It runs queries/fp_rate.sql in site_a (a
                                 few seconds) and also saves fp_rate.csv.

results.csv is appended to on every run of run_experiments.py, so for each
case (strategy, width, n, nestloop_off) we use the LAST row in the file.

Run from the project folder:
    source setup/env.example.sh
    .venv/bin/python bench/plot.py
"""

import csv
import os

import matplotlib
# "Agg" draws into files only, so no window (or display) is needed.
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import common


BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.normpath(os.path.join(BENCH_DIR, "..", "results"))
FP_RATE_SQL = os.path.normpath(os.path.join(BENCH_DIR, "..", "queries", "fp_rate.sql"))

STRATEGIES = ["baseline", "bloom_fixed10", "bloom_auto"]
WIDTHS = ["narrow", "wide"]

# One fixed color and marker per strategy, the same in every chart. The
# marker is a second cue besides the color, so the lines can be told apart
# in black-and-white prints and by colorblind readers too.
STRATEGY_COLORS = {
    "baseline": "#2a78d6",       # blue
    "bloom_fixed10": "#1baf7a",  # aqua
    "bloom_auto": "#eda100",     # yellow
}
STRATEGY_MARKERS = {
    "baseline": "o",
    "bloom_fixed10": "^",
    "bloom_auto": "D",
}
WIDTH_TITLES = {
    "narrow": "narrow rows (order_id, name)",
    "wide": "wide rows (order_id, name, padding)",
}
INK_COLOR = "#333333"
GRID_COLOR = "#e4e4e1"
SURFACE_COLOR = "#fcfcfb"


def read_latest_results():
    """
    Read results/results.csv and keep only the last row of each case.

    Input:  none.
    Output: a dict mapping (strategy, width, n, nestloop_off) to a row dict.
            n is an int and nestloop_off a bool; other values stay text.
    """
    latest = {}
    with open(os.path.join(RESULTS_DIR, "results.csv"), newline="") as csv_file:
        for row in csv.DictReader(csv_file):
            nestloop_off = row["nestloop_off"] == "True"
            key = (row["strategy"], row["width"], int(row["n"]), nestloop_off)
            latest[key] = row   # later rows replace earlier ones
    return latest


def read_sweep():
    """
    Read results/b_sweep.csv.

    Input:  none.
    Output: a list of row dicts (all values as text).
    """
    with open(os.path.join(RESULTS_DIR, "b_sweep.csv"), newline="") as csv_file:
        return list(csv.DictReader(csv_file))


def new_chart(title, x_label, y_label):
    """
    Create an empty chart with our common style: light background, a light
    grid behind the lines, no top/right border.

    Input:  title, x_label, y_label (texts, with units).
    Output: (figure, axes).
    """
    figure, axes = plt.subplots(figsize=(8, 5), dpi=120)
    figure.patch.set_facecolor(SURFACE_COLOR)
    axes.set_facecolor(SURFACE_COLOR)
    axes.set_title(title, color=INK_COLOR, fontsize=12, loc="left")
    axes.set_xlabel(x_label, color=INK_COLOR)
    axes.set_ylabel(y_label, color=INK_COLOR)
    axes.grid(True, which="major", color=GRID_COLOR, linewidth=0.8)
    axes.set_axisbelow(True)
    axes.spines["top"].set_visible(False)
    axes.spines["right"].set_visible(False)
    axes.tick_params(colors=INK_COLOR)
    return figure, axes


def save_chart(figure, axes, file_name):
    """
    Add the legend, save the chart as a PNG in results/ and close it.

    Input:  figure, axes; file_name, e.g. "fp_rate.png".
    Output: the full path of the saved file.
    """
    axes.legend(frameon=False, labelcolor=INK_COLOR)
    figure.tight_layout()
    path = os.path.join(RESULTS_DIR, file_name)
    figure.savefig(path, facecolor=SURFACE_COLOR)
    plt.close(figure)
    return path


def points_for(latest, strategy, width, column, nestloop_off=False):
    """
    The (n, value) points of one strategy and width, sorted by n.

    When nestloop_off is True we use the enable_nestloop = off row where one
    exists. For an n without such a row the normal row is used: that run
    was not a Nested Loop with a Join Filter in the first place, so it was
    not rerun.

    Input:  latest, from read_latest_results(); strategy; width; column,
            the CSV column to plot; nestloop_off.
    Output: (list of n values, list of float values).
    """
    n_values = sorted({key[2] for key in latest if key[0] == strategy and key[1] == width})
    x_values = []
    y_values = []
    for n in n_values:
        row = latest.get((strategy, width, n, nestloop_off))
        if row is None:
            row = latest[(strategy, width, n, False)]
        x_values.append(n)
        y_values.append(float(row[column]))
    return x_values, y_values


def plot_versus_n(latest, width, column, title, y_label, file_name, log_y):
    """
    One chart: a column of results.csv against n, one line per strategy.
    For time_ms we add a dashed line for every strategy that has runs redone
    with enable_nestloop = off (see run_experiments.py), if there are any.

    Input:  latest; width; column; title; y_label; file_name; log_y, True
            for a logarithmic y axis.
    Output: the saved file path.
    """
    figure, axes = new_chart(f"{title}, {WIDTH_TITLES[width]}",
                             "n = orders kept (order_id <= n), log scale", y_label)
    for strategy in STRATEGIES:
        x_values, y_values = points_for(latest, strategy, width, column)
        axes.plot(x_values, y_values, label=strategy, color=STRATEGY_COLORS[strategy],
                  marker=STRATEGY_MARKERS[strategy], markersize=7, linewidth=2)

    for strategy in STRATEGIES:
        has_nestloop_off_rows = any(key[0] == strategy and key[1] == width and key[3]
                                    for key in latest)
        if column == "time_ms" and has_nestloop_off_rows:
            x_values, y_values = points_for(latest, strategy, width, column, True)
            axes.plot(x_values, y_values, label=f"{strategy}, enable_nestloop = off",
                      color=STRATEGY_COLORS[strategy], marker=STRATEGY_MARKERS[strategy],
                      markerfacecolor="white", markersize=7, linewidth=2, linestyle="--")

    axes.set_xscale("log")
    if log_y:
        axes.set_yscale("log")
    return save_chart(figure, axes, f"{file_name}_{width}.png")


def plot_b_sweep(sweep_rows, width):
    """
    One chart: measured total bytes against b for one width variant, the
    total bytes predicted by the cost model, and a vertical dashed line at
    the predicted b*.

    Input:  sweep_rows, from read_sweep(); width.
    Output: the saved file path.
    """
    width_rows = [row for row in sweep_rows if row["width"] == width]
    b_values = [int(row["b"]) for row in width_rows]
    measured = [int(row["total_bytes"]) for row in width_rows]
    predicted = [int(row["model_total_bytes"]) for row in width_rows]
    predicted_b = float(width_rows[0]["predicted_b"])
    n = width_rows[0]["n"]

    figure, axes = new_chart(f"Estimated bytes on the wire vs bits per key b (n = {n}), "
                             f"{WIDTH_TITLES[width]}",
                             "b = Bloom filter bits per distinct key",
                             "bytes on the wire (estimated), A->B + B->A, log scale")
    axes.plot(b_values, measured, label="measured", color=STRATEGY_COLORS["bloom_auto"],
              marker="D", markersize=7, linewidth=2)
    axes.plot(b_values, predicted, label="cost model prediction", color=INK_COLOR,
              linewidth=1.2, linestyle=":")
    axes.axvline(predicted_b, color=INK_COLOR, linewidth=1.2, linestyle="--",
                 label=f"predicted b* = {predicted_b:.2f}")
    axes.set_yscale("log")
    return save_chart(figure, axes, f"total_bytes_vs_b_{width}.png")


def run_fp_rate_query():
    """
    Run queries/fp_rate.sql in site_a and save its rows to
    results/fp_rate.csv. The file is plain SQL (no psql commands), so we
    can send it as it is.

    Input:  none.
    Output: a list of (bits_per_key, measured_fp, predicted_fp) tuples.
    """
    with open(FP_RATE_SQL) as sql_file:
        sql_text = sql_file.read()

    conn = common.connect("site_a")
    rows = conn.execute(sql_text).fetchall()
    conn.close()

    points = []
    with open(os.path.join(RESULTS_DIR, "fp_rate.csv"), "w", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(["bits_per_key", "m", "k", "measured_fp", "predicted_fp"])
        for bits_per_key, m, k, measured_fp, predicted_fp in rows:
            writer.writerow([bits_per_key, m, k, measured_fp, predicted_fp])
            points.append((bits_per_key, float(measured_fp), float(predicted_fp)))
    return points


def plot_fp_rate(points):
    """
    One chart: measured and predicted false-positive rate against b.

    Input:  points, from run_fp_rate_query().
    Output: the saved file path.
    """
    b_values = [point[0] for point in points]
    measured = [point[1] for point in points]
    predicted = [point[2] for point in points]

    figure, axes = new_chart("Bloom filter false-positive rate: measured vs predicted\n"
                             "(10,000 keys in the filter, 1,000,000 non-member keys tested)",
                             "b = bits per key (k = round(0.693 b))",
                             "false-positive rate (fraction, log scale)")
    axes.plot(b_values, predicted, label="predicted (1 - e^(-kn/m))^k", color=INK_COLOR,
              linewidth=1.2, linestyle=":")
    axes.plot(b_values, measured, label="measured", color=STRATEGY_COLORS["baseline"],
              marker="o", markersize=7, linewidth=0)
    axes.set_yscale("log")
    return save_chart(figure, axes, "fp_rate.png")


def main():
    latest = read_latest_results()
    sweep_rows = read_sweep()

    saved = []
    for width in WIDTHS:
        saved.append(plot_versus_n(latest, width, "total_bytes",
                                   "Estimated bytes on the wire vs n",
                                   "bytes on the wire (estimated), A->B + B->A, log scale",
                                   "total_bytes_vs_n", True))
        saved.append(plot_versus_n(latest, width, "time_ms",
                                   "Query time vs n", "time in ms, median of 3 runs (log scale)",
                                   "time_ms_vs_n", True))
        saved.append(plot_versus_n(latest, width, "remote_rows",
                                   "Rows sent by site B vs n", "rows from site B (log scale)",
                                   "remote_rows_vs_n", True))
        saved.append(plot_b_sweep(sweep_rows, width))

    print("Running queries/fp_rate.sql in site_a (a few seconds) ...")
    saved.append(plot_fp_rate(run_fp_rate_query()))

    print("Saved:")
    for path in saved:
        print("  " + path)


if __name__ == "__main__":
    main()
