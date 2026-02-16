import re
import random


def roll_formula(formula: str, **vars) -> int:
    # все переменные, которых нет — 0
    for var in re.findall(r"\{(\w+)}", formula):
        if var not in vars:
            vars[var] = 0
    formula = formula.format(**vars)

    formula = formula.lower().replace("д", "d").replace("к", "d").replace(" ", "")

    match = re.search(r"\(([^)]+)\)", formula)
    if match:
        formula = match.group(1)

    dice_pattern = re.fullmatch(r'(\d+)[d](\d+)([+-]\d+)?', formula)
    if dice_pattern:
        n, sides, modifier = dice_pattern.groups()
        total = sum(random.randint(1, int(sides)) for _ in range(int(n)))
        if modifier:
            total += int(modifier)
        return total

    try:
        return int(formula)
    except ValueError:
        raise ValueError(f"Неверный формат броска: {formula}")