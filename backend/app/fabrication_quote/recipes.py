"""Pure time recipes. Seconds throughout; no implicit shop constants."""

from dataclasses import dataclass, field
from decimal import Decimal

from .schemas import BrakeRecipe, LaserRecipe, ManualRecipe, Recipe, WeldRecipe

ZERO = Decimal("0")


@dataclass
class RecipeTime:
    labor_seconds: Decimal | None
    machine_seconds: Decimal | None
    issues: list[tuple[str, str, str]] = field(default_factory=list)


def evaluate_recipe(recipe: Recipe) -> RecipeTime:
    issues: list[tuple[str, str, str]] = []

    def need(value, path):
        if value is None:
            issues.append(
                (
                    "missing_recipe_input",
                    path,
                    f"Required recipe input {path} is unknown.",
                )
            )
        return value

    if isinstance(recipe, ManualRecipe):
        return RecipeTime(
            need(recipe.labor_seconds, "labor_seconds"),
            need(recipe.machine_seconds, "machine_seconds"),
            issues,
        )
    if isinstance(recipe, LaserRecipe):
        machine = ZERO
        complete = True
        if not recipe.cuts:
            issues.append(
                (
                    "missing_cut_classes",
                    "cuts",
                    "Laser recipe requires at least one cut class, including explicit zero for a no-cut event.",
                )
            )
            complete = False
        for i, cut in enumerate(recipe.cuts):
            if cut.cut_length_mm > ZERO:
                if need(cut.speed_mm_per_second, f"cuts.{i}.speed_mm_per_second") is None:
                    complete = False
                else:
                    machine += cut.cut_length_mm / cut.speed_mm_per_second
            if cut.pierces:
                if need(cut.pierce_seconds, f"cuts.{i}.pierce_seconds") is None:
                    complete = False
                else:
                    machine += Decimal(cut.pierces) * cut.pierce_seconds
        if need(recipe.noncut_machine_seconds, "noncut_machine_seconds") is None:
            complete = False
        else:
            machine += recipe.noncut_machine_seconds
        if not recipe.speed_includes_dynamics:
            if need(recipe.dynamics_allowance_seconds, "dynamics_allowance_seconds") is None:
                complete = False
            else:
                machine += recipe.dynamics_allowance_seconds
        elif recipe.dynamics_allowance_seconds not in (None, ZERO):
            issues.append(
                (
                    "double_count_dynamics",
                    "dynamics_allowance_seconds",
                    "Dynamics are already included in effective speeds; remove the additional dynamics allowance.",
                )
            )
        return RecipeTime(
            need(recipe.labor_seconds, "labor_seconds"),
            machine if complete else None,
            issues,
        )
    if isinstance(recipe, BrakeRecipe):
        if not recipe.feasibility_reviewed:
            issues.append(
                (
                    "forming_review_required",
                    "feasibility_reviewed",
                    "Tooling, access, sequence and machine feasibility require estimator review.",
                )
            )
        hit_time = ZERO if recipe.hits == 0 else need(recipe.seconds_per_hit, "seconds_per_hit")
        handling = need(recipe.handling_seconds, "handling_seconds")
        inspection = need(recipe.inspection_seconds, "inspection_seconds")
        labor = (
            None
            if any(x is None for x in (hit_time, handling, inspection))
            else (Decimal(recipe.hits) * hit_time + handling + inspection) * Decimal(recipe.crew_size)
        )
        return RecipeTime(labor, need(recipe.machine_seconds, "machine_seconds"), issues)
    if isinstance(recipe, WeldRecipe):
        length = need(recipe.weld_length_mm, "weld_length_mm")
        # A travel-rate entry is not authority to omit the specified joint size.
        need(recipe.weld_size_mm, "weld_size_mm")
        speed = need(recipe.travel_speed_mm_per_second, "travel_speed_mm_per_second")
        nonlabor = need(recipe.nonweld_labor_seconds, "nonweld_labor_seconds")
        nonmachine = need(recipe.nonweld_machine_seconds, "nonweld_machine_seconds")
        arc = None if length is None or speed is None else length / speed
        labor = None if arc is None or nonlabor is None else (arc + nonlabor) * Decimal(recipe.crew_size)
        machine = None if arc is None or nonmachine is None else arc + nonmachine
        return RecipeTime(labor, machine, issues)
    raise TypeError("Unsupported recipe")
