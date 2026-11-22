def evaluate(rules: dict, answers: dict, citations: list[dict]) -> dict:
    checks = []
    missing = []

    def check(field: str, label: str, rule_text: str, predicate, required=True):
        value = answers.get(field)
        if value is None or value == "":
            if required:
                missing.append(label)
            checks.append({"field": field, "label": label, "provided": None, "result": "missing", "rule": rule_text})
            return
        passed = predicate(value)
        checks.append({"field": field, "label": label, "provided": value, "result": "meets_demo_rule" if passed else "does_not_meet_demo_rule", "rule": rule_text})

    if "age_min" in rules or "age_max" in rules:
        low, high = rules.get("age_min"), rules.get("age_max")
        check("age", "Age", f"Sample age range: {low if low is not None else 'no minimum'} to {high if high is not None else 'no maximum'}", lambda value: (low is None or int(value) >= low) and (high is None or int(value) <= high))
    if "states" in rules:
        allowed = rules["states"]
        check("state", "State", "Sample accepted state: " + ", ".join(allowed), lambda value: str(value).casefold() in {item.casefold() for item in allowed})
    if "annual_family_income_max" in rules:
        maximum = rules["annual_family_income_max"]
        check("annual_family_income", "Annual family income", f"Sample maximum: Rs. {maximum}", lambda value: float(value) <= maximum)
    if "education_levels" in rules:
        allowed = rules["education_levels"]
        check("education_level", "Education level", "Sample accepted levels: " + ", ".join(allowed), lambda value: str(value).casefold() in {item.casefold() for item in allowed})
    if "occupations" in rules:
        allowed = rules["occupations"]
        check("occupation", "Occupation", "Sample accepted occupations: " + ", ".join(allowed), lambda value: str(value).casefold() in {item.casefold() for item in allowed})
    if "employment_status" in rules:
        allowed = rules["employment_status"]
        check("employment_status", "Employment status", "Sample accepted status: " + ", ".join(allowed), lambda value: str(value).casefold() in {item.casefold() for item in allowed})

    failures = [item for item in checks if item["result"] == "does_not_meet_demo_rule"]
    if failures:
        result = "not_eligible"
    elif missing:
        result = "not_enough_information"
    elif rules.get("requires_manual_review"):
        result = "manual_review_required"
    else:
        result = "potentially_eligible"
    return {
        "result": result,
        "conditions": checks,
        "missing_information": missing,
        "failed_conditions": [item["label"] for item in failures],
        "citations": citations,
        "demo_only": False,
        "notice": "This is a comparison against the stored scheme rules, not an official eligibility decision or guarantee.",
    }
