import axios from 'axios';

const baseURL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';

const client = axios.create({
  baseURL,
  headers: { 'Content-Type': 'application/json' },
  // Session-cookie auth (multi-tenancy Phase 1): the backend sets an
  // httpOnly cookie; axios must opt in to sending it cross-origin.
  withCredentials: true,
});

// One place, all API modules inherit: a 401 on any feature endpoint means
// the session is gone — bounce to the login page.  /auth/* is exempt
// (RequireAuth and the login form handle their own 401s), as is the login
// page itself (no redirect loops).
client.interceptors.response.use(
  (response) => response,
  (error) => {
    const status = error?.response?.status;
    const url = error?.config?.url || '';
    if (
      status === 401 &&
      !url.startsWith('/auth') &&
      window.location.pathname !== '/login'
    ) {
      window.location.assign('/login');
    }
    return Promise.reject(error);
  }
);

export default client;
