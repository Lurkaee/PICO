import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { AppLayout } from '../components/layout/AppLayout';
import { ConnectivityProvider } from '../context/ConnectivityContext';
import fs from 'node:fs';
import path from 'node:path';

const mockMinimize = vi.fn().mockResolvedValue(undefined);
const mockClose = vi.fn().mockResolvedValue(undefined);
const mockIsTauri = vi.fn().mockReturnValue(false);

vi.mock('@tauri-apps/api/core', () => ({
  isTauri: () => mockIsTauri(),
}));

vi.mock('@tauri-apps/api/window', () => ({
  getCurrentWindow: () => ({
    minimize: mockMinimize,
    close: mockClose,
  }),
}));

describe('Desktop Shell & Window Controls (Milestone P11.2.1)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockIsTauri.mockReturnValue(false);
  });

  const defaultProps = {
    currentTab: 'chat' as const,
    onTabChange: vi.fn(),
    conversations: [],
    activeConversation: null,
    onSelectConversation: vi.fn(),
    onNewConversation: vi.fn(),
    pendingApprovalsCount: 0,
    sessionState: { paired: false, approver: '' },
    onOpenPairingModal: vi.fn(),
    classification: 'INTERNAL' as const,
  };

  it('renders standard browser layout without desktop window controls and marks header drag region', () => {
    mockIsTauri.mockReturnValue(false);

    render(
      <ConnectivityProvider>
        <AppLayout {...defaultProps}>
          <div>Content</div>
        </AppLayout>
      </ConnectivityProvider>
    );

    const header = screen.getByRole('banner');
    expect(header).toHaveAttribute('data-tauri-drag-region');
    expect(screen.queryByRole('group', { name: /window controls/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^minimize$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /close to tray/i })).not.toBeInTheDocument();
  });

  it('renders desktop window controls and wires minimize and close actions when isTauri() is true', async () => {
    mockIsTauri.mockReturnValue(true);

    render(
      <ConnectivityProvider>
        <AppLayout {...defaultProps}>
          <div>Content</div>
        </AppLayout>
      </ConnectivityProvider>
    );

    const minimizeBtn = await screen.findByRole('button', { name: /^minimize$/i });
    const closeBtn = await screen.findByRole('button', { name: /close to tray/i });

    expect(minimizeBtn).toBeInTheDocument();
    expect(closeBtn).toBeInTheDocument();

    // Minimize action
    fireEvent.click(minimizeBtn);
    await waitFor(() => {
      expect(mockMinimize).toHaveBeenCalledTimes(1);
    });

    // Close (hide to tray) action
    fireEvent.click(closeBtn);
    await waitFor(() => {
      expect(mockClose).toHaveBeenCalledTimes(1);
    });
  });

  describe('Tauri Capabilities & Security Configuration Guardrails', () => {
    it('enforces minimal, non-default capabilities in capabilities/default.json', () => {
      const capabilityPath = path.resolve(__dirname, '../../src-tauri/capabilities/default.json');
      const content = JSON.parse(fs.readFileSync(capabilityPath, 'utf-8'));

      // Must be scoped strictly to the main window
      expect(content.windows).toEqual(['main']);

      // Must never reintroduce the broad core:default group
      expect(content.permissions).not.toContain('core:default');

      // Must contain only the strict minimum permissions required by frontend JavaScript
      const allowedPermissions = [
        'core:window:allow-start-dragging',
        'core:window:allow-minimize',
        'core:window:allow-close',
      ];
      expect(content.permissions).toEqual(allowedPermissions);

      // Verify no unauthorized or dangerous capabilities exist
      for (const perm of content.permissions) {
        expect(perm).toMatch(/^core:window:allow-(start-dragging|minimize|close)$/);
      }
    });

    it('enforces strict Content Security Policy in tauri.conf.json', () => {
      const configPath = path.resolve(__dirname, '../../src-tauri/tauri.conf.json');
      const config = JSON.parse(fs.readFileSync(configPath, 'utf-8'));

      const security = config.app?.security;
      expect(security).toBeDefined();

      // Production CSP must not be null
      expect(security.csp).not.toBeNull();
      expect(typeof security.csp).toBe('string');

      // Production CSP must default to 'self' and disallow external/eval scripts
      expect(security.csp).toContain("default-src 'self'");
      expect(security.csp).toContain("script-src 'self'");
      expect(security.csp).not.toContain("'unsafe-eval'");
      // Production script-src must not allow 'unsafe-inline'
      expect(security.csp).not.toMatch(/script-src[^;]*'unsafe-inline'/);

      // Production CSP must allow React inline style attributes and Tauri IPC
      expect(security.csp).toContain("style-src 'self' 'unsafe-inline'");
      expect(security.csp).toContain('ipc:');

      // Development CSP must be configured for Vite HMR without leaking to production
      expect(security.devCsp).not.toBeNull();
      expect(typeof security.devCsp).toBe('string');
      expect(security.devCsp).toContain('localhost:1420');
      expect(security.devCsp).toContain('ws:');

      // Packaging/bundling must remain inactive in P11.2.1
      expect(config.bundle?.active).toBe(false);
    });
  });
});
