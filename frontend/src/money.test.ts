import { describe, expect, it } from "vitest";
import { formatFen, formatFenExact, formatPct, parseYuanToFen } from "./money.ts";

describe("formatFen", () => {
  it("drops the cents of whole yuan", () => {
    expect(formatFen(200000)).toBe("¥2,000");
    expect(formatFen(0)).toBe("¥0");
  });

  it("keeps two digits for fractional yuan", () => {
    expect(formatFen(8505)).toBe("¥85.05");
    expect(formatFen(150)).toBe("¥1.50");
    expect(formatFen(1)).toBe("¥0.01");
  });

  it("groups thousands and signs negatives", () => {
    expect(formatFen(123456789)).toBe("¥1,234,567.89");
    expect(formatFen(-1600000)).toBe("-¥16,000");
  });

  it("rejects values that are not integers", () => {
    expect(() => formatFen(12.5)).toThrow(RangeError);
  });
});

describe("formatFenExact", () => {
  it("always shows two decimals", () => {
    expect(formatFenExact(24500)).toBe("¥245.00");
    expect(formatFenExact(2500)).toBe("¥25.00");
    expect(formatFenExact(8505)).toBe("¥85.05");
    expect(formatFenExact(1234500)).toBe("¥12,345.00");
    expect(formatFenExact(-1600000)).toBe("-¥16,000.00");
  });
});

describe("parseYuanToFen", () => {
  it("reads whole and fractional yuan exactly", () => {
    expect(parseYuanToFen("1200")).toBe(120000);
    expect(parseYuanToFen("¥1,200.5")).toBe(120050);
    expect(parseYuanToFen("0.07")).toBe(7);
    expect(parseYuanToFen("19.99")).toBe(1999);
  });

  it("refuses zero, negatives, extra decimals and text", () => {
    expect(parseYuanToFen("0")).toBeNull();
    expect(parseYuanToFen("-5")).toBeNull();
    expect(parseYuanToFen("1.234")).toBeNull();
    expect(parseYuanToFen("一千")).toBeNull();
    expect(parseYuanToFen("")).toBeNull();
  });
});

describe("formatPct", () => {
  it("trims trailing zeros", () => {
    expect(formatPct("45.5000")).toBe("45.5%");
    expect(formatPct("26.7094")).toBe("26.7094%");
    expect(formatPct("40.0000")).toBe("40%");
    expect(formatPct("0.0000")).toBe("0%");
  });
});
