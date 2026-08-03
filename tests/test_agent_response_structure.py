from app.services.agent_defaults import normalize_response_policy, response_policy_prompt


def test_custom_widget_structure_is_included_with_high_priority() -> None:
    policy = normalize_response_policy(
        {
            "response_policy": {
                "custom_instructions": (
                    "Incluya una sección titulada Recomendación general y explique allí el dato decisivo."
                )
            }
        },
        "CROP_ANALYSIS",
    )

    prompt = response_policy_prompt(policy, "CROP_ANALYSIS")

    assert "PRIORIDAD ALTA PARA PRESENTACIÓN Y CONTENIDO" in prompt
    assert "Recomendación general" in prompt
    assert "Respeta literalmente nombres solicitados" in prompt


def test_custom_widget_instructions_allow_more_detailed_configuration() -> None:
    instructions = "x" * 5000
    policy = normalize_response_policy(
        {"response_policy": {"custom_instructions": instructions}},
        "CROP_ANALYSIS",
    )

    assert policy["custom_instructions"] == instructions
