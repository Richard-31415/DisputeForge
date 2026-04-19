"""Generate ablation bar chart — Pass Rate vs Escalation Recall.

  uv run python -m eval.ablation_chart
Saves: eval/ablation_chart.png
"""
import pathlib
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = REPO_ROOT / "eval" / "ablation_chart.png"

# ── Data — ordered ascending by pass rate ─────────────────────────────────────
CONFIGS = [
    ("raw model only",      60.0,  70.0),
    ("− policy RAG",        76.7,  80.0),
    ("− adversarial scan",  80.0,  70.0),
    ("− Reg E post-check",  83.3,  90.0),
    ("− evaluator rules",   83.3,  90.0),
    ("− ensemble",          90.0,  90.0),
    ("full harness",        93.3, 100.0),
]

labels      = [c[0] for c in CONFIGS]
pass_rates  = [c[1] for c in CONFIGS]
esc_recalls = [c[2] for c in CONFIGS]

n      = len(CONFIGS)
x      = np.arange(n)
width  = 0.35

# Colors — full harness gets accent, ablations get muted
ABLATION_PASS  = "#4C72B0"
ABLATION_ESC   = "#64B5CD"
FULL_PASS      = "#2CA02C"
FULL_ESC       = "#98DF8A"

pass_colors = [FULL_PASS if l == "full harness" else ABLATION_PASS for l in labels]
esc_colors  = [FULL_ESC  if l == "full harness" else ABLATION_ESC  for l in labels]

# ── Plot ──────────────────────────────────────────────────────────────────────
fig, ax = plt.subplots(figsize=(11, 6))
fig.patch.set_facecolor("#0F1117")
ax.set_facecolor("#0F1117")

bars_pass = ax.bar(x - width / 2, pass_rates, width, color=pass_colors, zorder=3)
bars_esc  = ax.bar(x + width / 2, esc_recalls, width, color=esc_colors,  zorder=3)

# Value labels
for bar in bars_pass:
    ax.text(
        bar.get_x() + bar.get_width() / 2,
        bar.get_height() + 1.2,
        f"{bar.get_height():.0f}%",
        ha="center", va="bottom", fontsize=9, color="white", fontweight="bold",
    )
for bar in bars_esc:
    ax.text(
        bar.get_x() + bar.get_width() / 2,
        bar.get_height() + 1.2,
        f"{bar.get_height():.0f}%",
        ha="center", va="bottom", fontsize=9, color="white", fontweight="bold",
    )

# Accuracy deploy gate: 90%
ax.axhline(90, color="#FFD700", linewidth=1.4, linestyle="--", zorder=4)
ax.text(0.08, 91.5, "accuracy gate  90%", color="#FFD700",
        fontsize=8.5, ha="left", style="italic", fontweight="bold")
# Escalation recall compliance floor: 95%
ax.axhline(95, color="#f87171", linewidth=1.4, linestyle="--", zorder=4)
ax.text(0.08, 96.5, "esc. recall floor  95%", color="#f87171",
        fontsize=8.5, ha="left", style="italic", fontweight="bold")

# Grid
ax.yaxis.grid(True, color="#2A2A3A", linewidth=0.7, zorder=0)
ax.set_axisbelow(True)

# Axes
ax.set_xticks(x)
ax.set_xticklabels(labels, color="white", fontsize=10)
ax.set_ylim(0, 105)
ax.set_ylabel("Percentage (%)", color="#AAAAAA", fontsize=11)
ax.tick_params(colors="#AAAAAA")
for spine in ax.spines.values():
    spine.set_edgecolor("#2A2A3A")

# Title
ax.set_title(
    "DisputeForge — Ablation Study + Ensemble  (30 cases, OpenAI)",
    color="white", fontsize=13, fontweight="bold", pad=14,
)

# Legend
legend_handles = [
    mpatches.Patch(color=ABLATION_PASS, label="Pass Rate (ablated)"),
    mpatches.Patch(color=ABLATION_ESC,  label="Escalation Recall (ablated)"),
    mpatches.Patch(color=FULL_PASS,     label="Pass Rate (full harness)"),
    mpatches.Patch(color=FULL_ESC,      label="Escalation Recall (full harness)"),
]
ax.legend(handles=legend_handles, loc="upper left", framealpha=0.2,
          facecolor="#1A1A2E", edgecolor="#444", labelcolor="white", fontsize=9)

plt.tight_layout()
fig.savefig(OUT, dpi=160, bbox_inches="tight", facecolor=fig.get_facecolor())
print(f"Saved → {OUT.relative_to(REPO_ROOT)}")
