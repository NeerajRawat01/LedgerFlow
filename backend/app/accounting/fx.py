"""FX rate infrastructure (Stage 1).

Loads the recorded rates from ``fx_rates.csv`` (via the existing
:class:`SourceDataService`) and resolves a conversion between two currencies
for a given policy rate type.

Resolution order, all using **the same rate type** as requested:

1. **Direct** — a recorded ``(base -> quote)`` rate for that type.
2. **Inverse** — a recorded ``(quote -> base)`` rate of that type, used as
   ``1 / rate``. Justified because the source ``convention`` column states
   the rate as "X per 1 base", so the reciprocal is the exact same-type
   inverse quote (confirmed by policy review).
3. **Cross** — no direct or inverse pair, but a single intermediate currency
   connects them: ``rate(base -> quote) = rate(base -> via) * rate(via -> quote)``
   where each leg is itself resolved direct-or-inverse. This implements the
   policy rule "use the product of the two given rates of the same type
   (for example, GBP/INR closing = GBP/USD closing x USD/INR closing)".

A **missing** rate is a typed :class:`MissingFXRateError` — never a number.
A recorded **zero** rate is a valid Decimal zero, distinguishable from
missing; inverting a zero rate is impossible and raises
:class:`MissingFXRateError` (no fabricated rate).

No rate is ever invented: every returned Decimal is derived only from values
present in the source file.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.accounting.errors import AmbiguousFXRateError, MissingFXRateError
from app.accounting.models import FXRate, FXRateType
from app.services.source_data import SourceDataService

_ONE = Decimal("1")


def _load_rate(value: str) -> Decimal:
    return Decimal(value.strip())


@dataclass(frozen=True)
class ResolvedRate:
    """A resolved conversion plus how it was obtained (for the audit trail)."""

    base_currency: str
    quote_currency: str
    rate_type: FXRateType
    rate: Decimal
    method: str  # "direct" | "inverse" | "cross"
    legs: tuple[FXRate, ...] = ()
    via: str | None = None


class FXService:
    """Resolve currency conversions from the recorded FX rate table."""

    def __init__(self, service: SourceDataService | None = None) -> None:
        self.service = service if service is not None else SourceDataService()
        self._rates: list[FXRate] | None = None

    # -- loading -----------------------------------------------------------

    @property
    def rates(self) -> list[FXRate]:
        """Every recorded rate, parsed once and cached."""
        if self._rates is None:
            rows = self.service.get_fx_rates()
            self._rates = [
                FXRate(
                    base_currency=r["base_currency"].strip().upper(),
                    quote_currency=r["quote_currency"].strip().upper(),
                    rate_type=FXRateType(r["rate_type"].strip().lower()),
                    rate=_load_rate(r["rate"]),
                    as_of=r.get("as_of", "").strip(),
                    convention=r.get("convention", "").strip(),
                    source_row=index + 1,
                )
                for index, r in enumerate(rows)
            ]
        return self._rates

    def currencies(self) -> list[str]:
        """Distinct currencies appearing as either base or quote."""
        seen: set[str] = set()
        for rate in self.rates:
            seen.add(rate.base_currency)
            seen.add(rate.quote_currency)
        return sorted(seen)

    # -- lookup helpers ----------------------------------------------------

    def _find(self, base: str, quote: str, rate_type: FXRateType) -> list[FXRate]:
        """All recorded rates for an exact ordered pair and type."""
        return [
            r
            for r in self.rates
            if r.base_currency == base
            and r.quote_currency == quote
            and r.rate_type == rate_type
        ]

    def _single(self, base: str, quote: str, rate_type: FXRateType) -> FXRate | None:
        matches = self._find(base, quote, rate_type)
        if len(matches) > 1:
            raise AmbiguousFXRateError(
                f"multiple {rate_type.value} rates recorded for {base}/{quote}",
                details={
                    "base_currency": base,
                    "quote_currency": quote,
                    "rate_type": rate_type.value,
                    "source_rows": [m.source_row for m in matches],
                },
            )
        return matches[0] if matches else None

    # -- resolution --------------------------------------------------------

    def resolve(
        self,
        base_currency: str,
        quote_currency: str,
        rate_type: FXRateType | str,
    ) -> ResolvedRate:
        """Resolve ``base_currency -> quote_currency`` for ``rate_type``.

        Raises:
            MissingFXRateError: no direct, inverse or cross rate exists, or an
                inverse/cross leg would require dividing by a zero rate.
            AmbiguousFXRateError: the source records two distinct rates for
                the same ordered pair and type.
        """
        base = base_currency.strip().upper()
        quote = quote_currency.strip().upper()
        rtype = FXRateType(rate_type.value if isinstance(rate_type, FXRateType) else str(rate_type).lower())

        if base == quote:
            return ResolvedRate(
                base_currency=base,
                quote_currency=quote,
                rate_type=rtype,
                rate=_ONE,
                method="identity",
            )

        # 1. Direct.
        direct = self._single(base, quote, rtype)
        if direct is not None:
            return ResolvedRate(
                base_currency=base,
                quote_currency=quote,
                rate_type=rtype,
                rate=direct.rate,
                method="direct",
                legs=(direct,),
            )

        # 2. Inverse (1 / reverse rate).
        inverse = self._single(quote, base, rtype)
        if inverse is not None:
            if inverse.rate == 0:
                raise MissingFXRateError(
                    f"cannot invert a zero {rtype.value} rate for {quote}/{base}",
                    details={
                        "base_currency": base,
                        "quote_currency": quote,
                        "rate_type": rtype.value,
                        "reason": "zero_rate_inverse",
                        "source_row": inverse.source_row,
                    },
                )
            return ResolvedRate(
                base_currency=base,
                quote_currency=quote,
                rate_type=rtype,
                rate=_ONE / inverse.rate,
                method="inverse",
                legs=(inverse,),
            )

        # 3. Cross-rate via a shared intermediate currency.
        for via in self._cross_candidates(base, quote):
            first = self._try_convert(base, via, rtype)
            second = self._try_convert(via, quote, rtype)
            if first is not None and second is not None:
                return ResolvedRate(
                    base_currency=base,
                    quote_currency=quote,
                    rate_type=rtype,
                    rate=first.rate * second.rate,
                    method="cross",
                    legs=first.legs + second.legs,
                    via=via,
                )

        raise MissingFXRateError(
            f"no {rtype.value} rate to convert {base} to {quote}",
            details={
                "base_currency": base,
                "quote_currency": quote,
                "rate_type": rtype.value,
                "recorded_pairs": sorted(
                    f"{r.base_currency}/{r.quote_currency}" for r in self.rates
                ),
            },
        )

    def _cross_candidates(self, base: str, quote: str) -> list[str]:
        """Currencies that appear on both ends of either requested currency."""
        connected: set[str] = set()
        for rate in self.rates:
            for currency in (rate.base_currency, rate.quote_currency):
                if currency not in (base, quote):
                    connected.add(currency)
        return sorted(connected)

    def _try_convert(
        self, base: str, quote: str, rate_type: FXRateType
    ) -> ResolvedRate | None:
        """Direct-or-inverse resolution for one leg; ``None`` when absent."""
        if base == quote:
            return ResolvedRate(base, quote, rate_type, _ONE, "identity")
        direct = self._single(base, quote, rate_type)
        if direct is not None:
            return ResolvedRate(base, quote, rate_type, direct.rate, "direct", (direct,))
        inverse = self._single(quote, base, rate_type)
        if inverse is not None and inverse.rate != 0:
            return ResolvedRate(
                base, quote, rate_type, _ONE / inverse.rate, "inverse", (inverse,)
            )
        return None

    # -- convenience -------------------------------------------------------

    def convert(
        self,
        amount: Decimal,
        base_currency: str,
        quote_currency: str,
        rate_type: FXRateType | str,
    ) -> Decimal:
        """Convert ``amount`` at the resolved rate, at full precision.

        No rounding is applied here — the policy rounds only reported figures
        (``models.round_reported``).
        """
        resolved = self.resolve(base_currency, quote_currency, rate_type)
        return amount * resolved.rate
