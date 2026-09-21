/**
 * A hand-rolled `fetch` double.
 *
 * Hand-rolled rather than MSW: the whole surface is one origin's JSON routes,
 * and a route table keyed by method and path is both smaller than a service
 * worker and able to assert on the exact request the client built.
 */

/** One recorded request. */
export interface RecordedRequest {
  method: string;
  url: string;
  path: string;
  query: URLSearchParams;
  body: unknown;
  headers: Headers;
}

/** What a route handler may answer with. */
export type RouteResponse =
  | { status?: number; body?: unknown }
  | ((request: RecordedRequest) => { status?: number; body?: unknown });

/** The route table: `"<METHOD> <path>"` to a response. */
export type Routes = Record<string, RouteResponse>;

/** What {@link mockFetch} returns. */
export interface MockFetch {
  /** The double to hand the client. */
  fetch: typeof globalThis.fetch;
  /** Every request made, in order. */
  requests: RecordedRequest[];
  /** Replaces or adds a route mid-test. */
  route: (key: string, response: RouteResponse) => void;
}

/** Parses a request body back into a value, or undefined when there is none. */
function readBody(init: RequestInit | undefined): unknown {
  const body = init?.body;
  if (body === undefined || body === null) {
    return undefined;
  }
  if (typeof body === 'string') {
    try {
      return JSON.parse(body);
    } catch {
      return body;
    }
  }
  return body;
}

/**
 * A `fetch` serving a route table.
 *
 * An unrouted request answers 404 with the shared error envelope, so a missing
 * route fails the test that needed it rather than hanging.
 */
export function mockFetch(routes: Routes = {}): MockFetch {
  const table = { ...routes };
  const requests: RecordedRequest[] = [];

  const fetchDouble: typeof globalThis.fetch = (input, init) => {
    const url =
      typeof input === 'string'
        ? input
        : input instanceof URL
          ? input.toString()
          : input.url;
    const method = (init?.method ?? 'GET').toUpperCase();
    const parsed = new URL(url, 'https://api.test');
    const record: RecordedRequest = {
      method,
      url,
      path: parsed.pathname,
      query: parsed.searchParams,
      body: readBody(init),
      headers: new Headers(init?.headers),
    };
    requests.push(record);

    const handler = table[`${method} ${parsed.pathname}`];
    if (handler === undefined) {
      return Promise.resolve(
        new Response(
          JSON.stringify({
            success: false,
            status: 404,
            message: `No route for ${method} ${parsed.pathname}.`,
            request_id: 'r-test',
          }),
          { status: 404, headers: { 'content-type': 'application/json' } }
        )
      );
    }
    const answer = typeof handler === 'function' ? handler(record) : handler;
    const status = answer.status ?? 200;
    return Promise.resolve(
      new Response(
        status === 204 || answer.body === undefined
          ? null
          : JSON.stringify(answer.body),
        { status, headers: { 'content-type': 'application/json' } }
      )
    );
  };

  return {
    fetch: fetchDouble,
    requests,
    route: (key, response) => {
      table[key] = response;
    },
  };
}
