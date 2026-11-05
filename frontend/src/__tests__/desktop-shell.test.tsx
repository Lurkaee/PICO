import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { AppLayout } from '../components/layout/AppLayout';
import { ConnectivityProvider } from '../context/ConnectivityContext';

const mockMinimize = vi.fn().mockResolvedValue(undefined);
const mockClose = vi.fn().mockResolvedValue(undefined);

vi.mock('@tauri-apps/api/window', () => ({
  getCurrentWindow: () => ({
    minimize: mockMinimize,
    close: mockClose,
  }),
}));

describe('Desktop Shell & Window Controls (Milestone P11.2.1)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    delete (window as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
  });

  afterEach(() => {
    delete (window as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
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

  it('renders desktop window controls and wires minimize and close actions when running inside Tauri', async () => {
    // Simulate Tauri runtime environment
    (window as unknown as { __TAURI_INTERNALS__: Record<string, unknown> }).__TAURI_INTERNALS__ = {};

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
});
