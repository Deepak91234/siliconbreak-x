import type { FaultInfo } from "../api/types";

/**
 * Fault-injection choices for a design family (GET /api/faults). A fault with
 * an empty `families` list applies to every family. With no family known yet
 * (a new design that has not been interpreted) every fault is offered.
 */
export function faultsForFamily(faults: FaultInfo[] | null, family: string | null): FaultInfo[] | null {
  if (!faults || !family) return faults;
  return faults.filter((f) => !f.families?.length || f.families.includes(family));
}
