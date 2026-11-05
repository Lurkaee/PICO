import '@testing-library/jest-dom/vitest';
import type { TestingLibraryMatchers } from '@testing-library/jest-dom/matchers';
import { vi } from 'vitest';

// Workaround for upstream Vitest 5 type incompatibility in @testing-library/jest-dom
// Ref: https://github.com/testing-library/jest-dom/issues/738
// Ref: https://github.com/testing-library/jest-dom/pull/742
declare module 'vitest' {
  interface Matchers<R, T> extends TestingLibraryMatchers<unknown, R> {}
}

window.HTMLElement.prototype.scrollIntoView = vi.fn();
