"""Calculo de gasto calorico segun la formula Keytel (sportscience)."""


def keytel_kcal_per_min(hr: float, age: int, weight_kg: float, sex: str) -> float:
    """kcal/min estimadas a partir de FC, edad, peso y sexo.

    Formula Keytel:
      Hombre: (-55.0969 + 0.6309*FC + 0.1988*peso + 0.2017*edad) / 4.444
      Mujer:  (-20.4022 + 0.4472*FC + 0.1213*peso + 0.1574*edad) / 4.444
    """
    if sex.upper().startswith("M"):
        kcal = (-55.0969 + 0.6309 * hr + 0.1988 * weight_kg + 0.2017 * age) / 4.444
    else:
        kcal = (-20.4022 + 0.4472 * hr + 0.1213 * weight_kg + 0.1574 * age) / 4.444
    return max(0.0, kcal)
