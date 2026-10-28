import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { MemoryInspector } from '../components/memory/MemoryInspector';
import { api } from '../api/client';

vi.mock('../api/client', () => ({
  api: {
    getSemanticFacts: vi.fn(),
    searchSemanticFacts: vi.fn(),
    forgetSemanticFact: vi.fn(),
    getWorkingMemory: vi.fn(),
    getProceduralMemory: vi.fn(),
    getMemoryStatus: vi.fn(),
  },
}));

describe('MemoryInspector Component', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('refreshes the current search and clears to an unfiltered list', async () => {
    vi.mocked(api.getSemanticFacts).mockResolvedValue([]);
    vi.mocked(api.searchSemanticFacts).mockResolvedValue([]);
    render(<MemoryInspector />);
    await waitFor(() => expect(screen.getByText('Refresh')).not.toBeDisabled());

    const input = screen.getByPlaceholderText(/Search semantic memory/);
    fireEvent.change(input, { target: { value: '  Python  ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Search' }));
    await waitFor(() => expect(api.searchSemanticFacts).toHaveBeenCalledWith('Python', 20));
    await waitFor(() => expect(screen.getByText('Refresh')).not.toBeDisabled());
    fireEvent.click(screen.getByText('Refresh'));
    await waitFor(() => expect(api.searchSemanticFacts).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByText('Refresh')).not.toBeDisabled());

    fireEvent.click(screen.getByText('Clear'));
    await waitFor(() => expect(api.getSemanticFacts).toHaveBeenCalledTimes(2));
    expect(api.getSemanticFacts).toHaveBeenLastCalledWith(50, 0);
    expect(api.searchSemanticFacts).toHaveBeenCalledTimes(2);
    expect(input).toHaveValue('');
  });

  it('renders semantic facts and distinguishes observed truth from inferred hypotheses', async () => {
    vi.mocked(api.getSemanticFacts).mockResolvedValueOnce([
      {
        id: 'fact_1',
        subject: 'User',
        predicate: 'prefers_language',
        object: 'Python',
        confidence: 1.0,
        source_provenance: 'explicit_user_statement',
        data_classification: 'INTERNAL',
        verified: true,
        is_inferred: false,
        created_at: '2026-10-22T08:00:00Z',
      },
      {
        id: 'fact_2',
        subject: 'User',
        predicate: 'likely_working_on',
        object: 'FastAPI Backend',
        confidence: 0.72,
        source_provenance: 'file_edit_inference',
        data_classification: 'INTERNAL',
        verified: false,
        is_inferred: true,
        created_at: '2026-10-22T08:05:00Z',
      },
    ]);

    render(<MemoryInspector />);

    await waitFor(() => {
      expect(screen.getByText('Python')).toBeInTheDocument();
      expect(screen.getByText('FastAPI Backend')).toBeInTheDocument();
    });

    expect(screen.getByText('Observed Ground Truth')).toBeInTheDocument();
    expect(screen.getByText('Inferred Hypothesis')).toBeInTheDocument();
  });

  it('requires confirmation before forgetting a semantic fact and reflects deletion', async () => {
    vi.mocked(api.getSemanticFacts).mockResolvedValueOnce([
      {
        id: 'fact_to_forget',
        subject: 'Operator',
        predicate: 'temporary_note',
        object: 'Meeting at 3PM',
        confidence: 0.9,
        source_provenance: 'note',
        data_classification: 'INTERNAL',
        verified: true,
        is_inferred: false,
        created_at: '2026-10-22T08:00:00Z',
      },
    ]);

    vi.mocked(api.forgetSemanticFact).mockResolvedValueOnce({ forgotten: true });

    render(<MemoryInspector />);

    await waitFor(() => {
      expect(screen.getByText('Meeting at 3PM')).toBeInTheDocument();
    });

    // Click trash button
    const trashBtn = screen.getByLabelText('Forget fact');
    fireEvent.click(trashBtn);

    // Confirmation prompt appears
    expect(screen.getByText('Confirm forget?')).toBeInTheDocument();
    expect(api.forgetSemanticFact).not.toHaveBeenCalled();

    // Mock next fetch after deletion returns empty list
    vi.mocked(api.getSemanticFacts).mockResolvedValueOnce([]);

    // Click Forget confirmation button
    const confirmBtn = screen.getByRole('button', { name: 'Forget' });
    fireEvent.click(confirmBtn);

    await waitFor(() => {
      expect(api.forgetSemanticFact).toHaveBeenCalledWith('fact_to_forget');
    });
  });
it('ensures Clear is authoritative and earlier pending searches resolving after Clear are discarded', async () => {
    // Initial load
    vi.mocked(api.getSemanticFacts).mockResolvedValueOnce([
      {
        id: 'initial_fact',
        subject: 'System',
        predicate: 'status',
        object: 'operational',
        confidence: 1.0,
        source_provenance: 'bootstrap',
        data_classification: 'INTERNAL',
        verified: true,
        is_inferred: false,
        created_at: '2026-10-22T08:00:00Z',
      },
    ]);

    render(<MemoryInspector />);
    await waitFor(() => expect(screen.getByText('operational')).toBeInTheDocument());

    // Setup deferred promise for a slow search
    let resolveSlowSearch: ((val: any) => void) | undefined;
    const slowSearchPromise = new Promise((resolve) => {
      resolveSlowSearch = resolve;
    });
    vi.mocked(api.searchSemanticFacts).mockImplementationOnce(() => slowSearchPromise as any);

    // Trigger slow search
    const input = screen.getByPlaceholderText(/Search semantic memory/);
    fireEvent.change(input, { target: { value: 'slow query' } });
    fireEvent.click(screen.getByRole('button', { name: 'Search' }));

    expect(api.searchSemanticFacts).toHaveBeenCalledWith('slow query', 20);

    // Clear request will return fresh unfiltered facts immediately
    vi.mocked(api.getSemanticFacts).mockResolvedValueOnce([
      {
        id: 'unfiltered_fact_after_clear',
        subject: 'Authoritative',
        predicate: 'truth',
        object: 'unfiltered_result',
        confidence: 1.0,
        source_provenance: 'clear_query',
        data_classification: 'INTERNAL',
        verified: true,
        is_inferred: false,
        created_at: '2026-10-22T08:10:00Z',
      },
    ]);

    // Click Clear
    const clearBtn = screen.getByRole('button', { name: 'Clear' });
    fireEvent.click(clearBtn);

    // Wait for Clear result to populate
    await waitFor(() => {
      expect(screen.getByText('unfiltered_result')).toBeInTheDocument();
    });

    // Now resolve the earlier slow search with stale results
    await act(async () => {
      resolveSlowSearch?.([
        {
          id: 'stale_search_fact',
          subject: 'Stale',
          predicate: 'query',
          object: 'stale_search_result',
          confidence: 0.5,
          source_provenance: 'stale_search',
          data_classification: 'INTERNAL',
          verified: false,
          is_inferred: true,
          created_at: '2026-10-22T08:05:00Z',
        },
      ]);
      await Promise.resolve();
    });

    // Verify stale results do NOT overwrite authoritative clear results
    expect(screen.getByText('unfiltered_result')).toBeInTheDocument();
    expect(screen.queryByText('stale_search_result')).not.toBeInTheDocument();
    expect(input).toHaveValue('');
  });
});
