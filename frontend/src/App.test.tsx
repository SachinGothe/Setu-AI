import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App from './App'

afterEach(() => {
  vi.unstubAllGlobals()
  window.history.replaceState({}, '', '/')
  localStorage.removeItem('civicpilot_access_token')
})

function mockAuthenticatedFetch(role: 'admin' | 'user') {
  const user = { id: 'user-1', email: 'account@example.test', display_name: 'Account', role }
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    let body: unknown = {}
    if (url.endsWith('/api/v1/health')) body = { status: 'ok', mode: 'local_demo' }
    else if (url.endsWith('/api/v1/auth/login')) body = { access_token: 'demo-token', user: { ...user, role: role === 'admin' ? 'user' : 'admin' } }
    else if (url.endsWith('/api/v1/auth/me')) body = user
    else if (url.endsWith('/api/v1/admin/overview')) body = { counts: { total_registered_users: 4, total_uploaded_legal_documents: 2, total_knowledge_documents: 1, total_sample_schemes: 3 }, recent_activities: [] }
    else if (url.endsWith('/api/v1/admin/users')) body = { items: [{ id: 'user-2', email: 'reader@example.test', display_name: 'Reader', role: 'user', is_active: true, created_at: '2026-01-01' }] }
    else if (url.endsWith('/api/v1/admin/knowledge-documents')) body = { items: [] }
    else if (url.endsWith('/api/v1/admin/audit-logs?limit=200')) body = { items: [] }
    else if (url.endsWith('/api/v1/schemes')) body = { items: [] }
    return { ok: true, status: 200, headers: new Headers({ 'content-type': 'application/json' }), json: async () => body } as Response
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

async function signIn(role: 'admin' | 'user' = 'user') {
  window.history.replaceState({}, '', '/')
  render(<App />)
  fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'account@example.test' } })
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'demo-passphrase' } })
  fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
  if (role === 'admin') await screen.findByRole('heading', { name: 'Admin Dashboard' })
  else await screen.findByRole('heading', { name: 'Your next step, made clearer.' })
}

describe('CivicPilot App', () => {
  it('renders the independent service dashboard', () => {
    render(<App />)
    expect(screen.getByRole('heading', { name: 'Sign in' })).toBeTruthy()
    expect(screen.getByText(/Secure local account/i)).toBeTruthy()
  })

  it('provides public account creation without an admin role selector', () => {
    render(<App />)
    expect(screen.getByRole('link', { name: /Sign up/i })).toBeTruthy()
    expect(screen.getByText(/not a government website/i)).toBeTruthy()
  })

  it('shows and opens the Admin Dashboard for admin-role accounts', async () => {
    const fetchMock = mockAuthenticatedFetch('admin')
    await signIn('admin')
    expect(await screen.findByRole('heading', { name: 'Admin Dashboard' })).toBeTruthy()
    expect(screen.getByText('Registered users')).toBeTruthy()
    expect(screen.getByText('4')).toBeTruthy()
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining('/api/v1/admin/overview'), expect.anything())
  })

  it('opens the user and legal repository management sections', async () => {
    mockAuthenticatedFetch('admin')
    await signIn('admin')
    await screen.findByRole('heading', { name: 'Admin Dashboard' })
    fireEvent.click(screen.getByRole('tab', { name: 'Users' }))
    expect(await screen.findByText('Reader · reader@example.test')).toBeTruthy()
    expect(screen.getByLabelText('Role for reader@example.test')).toBeTruthy()
    fireEvent.click(screen.getByRole('tab', { name: 'Legal knowledge' }))
    expect(await screen.findByRole('heading', { name: 'Legal Knowledge Repository' })).toBeTruthy()
    expect(screen.getByLabelText('Issuing authority')).toBeTruthy()
    expect(screen.queryByRole('tab', { name: 'Schemes' })).toBeNull()
  })

  it('hides admin navigation and denies direct admin route access to normal users', async () => {
    const fetchMock = mockAuthenticatedFetch('user')
    await signIn('user')
    expect(screen.queryByRole('link', { name: 'Admin' })).toBeNull()
    window.history.pushState({}, '', '/admin')
    window.dispatchEvent(new PopStateEvent('popstate'))
    expect(await screen.findByRole('heading', { name: 'Your next step, made clearer.' })).toBeTruthy()
    await waitFor(() => expect(fetchMock).not.toHaveBeenCalledWith(expect.stringContaining('/api/v1/admin/overview'), expect.anything()))
  })

  it('clears a revoked in-memory session after the backend returns 401', async () => {
    const user = { id: 'user-1', email: 'account@example.test', display_name: 'Account', role: 'admin' }
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/api/v1/health')) return { ok: true, status: 200, json: async () => ({ status: 'ok' }) } as Response
      if (url.endsWith('/api/v1/auth/login')) return { ok: true, status: 200, json: async () => ({ access_token: 'revoked-token', user }) } as Response
      return { ok: false, status: 401, json: async () => ({ detail: 'Session revoked' }) } as Response
    })
    vi.stubGlobal('fetch', fetchMock)
    window.history.replaceState({}, '', '/')
    render(<App />)
    fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'account@example.test' } })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'demo-passphrase' } })
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining('/api/v1/auth/me'), expect.anything()))
    expect(screen.getByRole('heading', { name: 'Sign in' })).toBeTruthy()
    expect(screen.queryByRole('link', { name: 'Admin' })).toBeNull()
  })

  it('refreshes the current role from the server when the tab regains focus', async () => {
    let currentRole: 'admin' | 'user' = 'user'
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      const user = { id: 'user-1', email: 'account@example.test', display_name: 'Account', role: currentRole }
      let body: unknown = {}
      if (url.endsWith('/api/v1/health')) body = { status: 'ok' }
      else if (url.endsWith('/api/v1/auth/login')) body = { access_token: 'demo-token', user: { ...user, role: currentRole === 'admin' ? 'user' : 'admin' } }
      else if (url.endsWith('/api/v1/auth/me')) body = user
      return { ok: true, status: 200, headers: new Headers({ 'content-type': 'application/json' }), json: async () => body } as Response
    })
    vi.stubGlobal('fetch', fetchMock)
    await signIn('user')
    expect(screen.queryByRole('link', { name: 'Admin' })).toBeNull()
    currentRole = 'admin'
    window.dispatchEvent(new Event('focus'))
    expect(await screen.findByRole('link', { name: 'Admin' })).toBeTruthy()
  })

  it('restores the session token after reload and reads the role from the backend', async () => {
    localStorage.setItem('civicpilot_access_token', 'persisted-session-token')
    const fetchMock = mockAuthenticatedFetch('admin')
    window.history.replaceState({}, '', '/')
    render(<App />)
    expect(await screen.findByRole('heading', { name: 'Admin Dashboard' })).toBeTruthy()
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining('/api/v1/auth/me'), expect.objectContaining({
      headers: expect.objectContaining({ Authorization: 'Bearer persisted-session-token' }),
    }))
  })

  it('supports password visibility on the shared login/signup page', () => {
    render(<App />)
    const password = screen.getByLabelText('Password') as HTMLInputElement
    expect(password.type).toBe('password')
    fireEvent.click(screen.getByRole('button', { name: 'Show password' }))
    expect((screen.getByLabelText('Password') as HTMLInputElement).type).toBe('text')
  })

})
