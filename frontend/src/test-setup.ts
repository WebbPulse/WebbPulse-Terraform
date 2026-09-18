/// <reference types="@testing-library/jest-dom" />
import { expect, afterEach } from 'vitest';
import { cleanup } from '@testing-library/react';
import * as matchers from '@testing-library/jest-dom/matchers';

expect.extend(matchers);

declare module 'vitest' {
  /* eslint-disable @typescript-eslint/no-empty-object-type, @typescript-eslint/no-unused-vars */
  interface Matchers<
    R extends void | Promise<void> = void | Promise<void>,
    T = unknown,
  > extends matchers.TestingLibraryMatchers<unknown, R> {}
  /* eslint-enable @typescript-eslint/no-empty-object-type, @typescript-eslint/no-unused-vars */
}

afterEach(() => {
  cleanup();
});
