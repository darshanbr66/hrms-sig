/** RFC 9457 problem details as the API sends them (docs/api-architecture.md §4). */

export type ProblemType =
  | "unauthenticated"
  | "invalid-credentials"
  | "forbidden"
  | "step-up-required"
  | "mfa-enrolment-required"
  | "not-found"
  | "conflict"
  | "precondition-failed"
  | "validation-error"
  | "rate-limited"
  | "internal-error"
  | "temporarily-unavailable";

export interface FieldError {
  field: string | null;
  code: string;
  message: string;
}

export interface Problem {
  type: string;
  title: string;
  status: number;
  detail: string | null;
  request_id: string | null;
  code?: string;
  errors?: FieldError[];
  max_age_seconds?: number;
}

/** A failed API call. The UI shows `message`, never internal details. */
export class ApiError extends Error {
  readonly problem: Problem;
  readonly retryAfterSeconds: number | null;

  constructor(problem: Problem, retryAfterSeconds: number | null = null) {
    super(problem.detail ?? problem.title);
    this.name = "ApiError";
    this.problem = problem;
    this.retryAfterSeconds = retryAfterSeconds;
  }

  is(type: ProblemType): boolean {
    return this.problem.type === `/problems/${type}`;
  }

  get code(): string | undefined {
    return this.problem.code;
  }

  get requestId(): string | null {
    return this.problem.request_id;
  }
}

export function isProblem(value: unknown): value is Problem {
  return (
    typeof value === "object" &&
    value !== null &&
    "type" in value &&
    "title" in value &&
    "status" in value &&
    typeof (value as { type: unknown }).type === "string"
  );
}

/** A problem for failures that never reached the API (network down, unreadable response). */
export function networkProblem(): Problem {
  return {
    type: "about:blank",
    title: "We couldn't reach the HRMS. Check your connection, then try again.",
    status: 0,
    detail: null,
    request_id: null,
  };
}

export function toApiError(error: unknown, response: Response): ApiError {
  const retryAfter = Number(response.headers.get("Retry-After"));
  const problem = isProblem(error) ? error : networkProblem();
  return new ApiError(problem, Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter : null);
}
