const $ = id => document.getElementById(id);
const L = window.BioGateLocale;
let passwordVisible = false;

function renderPasswordToggle() {
  $('login-password').type = passwordVisible ? 'text' : 'password';
  $('toggle-password').textContent = L.t(passwordVisible ? 'hidePassword' : 'showPassword');
}

function showError(reason) {
  $('login-error').textContent = L.t(reason);
}

$('toggle-password').onclick = () => {
  passwordVisible = !passwordVisible;
  renderPasswordToggle();
  $('login-password').focus();
};

$('admin-login-form').onsubmit = async event => {
  event.preventDefault();
  const submit = $('login-submit');
  const username = $('login-username').value.trim();
  const password = $('login-password').value;
  if (!username || !password) return showError('INVALID_ADMIN_CREDENTIALS');
  submit.disabled = true;
  submit.textContent = L.t('signingIn');
  showError('');
  try {
    const response = await fetch('/api/admin/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail?.reason_code || 'REQUEST_FAILED');
    const next = new URLSearchParams(location.search).get('next');
    location.href = next?.startsWith('/admin') && !next.startsWith('/admin/login') ? next : '/admin';
  } catch (error) {
    showError(error.message);
    $('login-password').value = '';
    $('login-password').focus();
  } finally {
    submit.disabled = false;
    submit.textContent = L.t('login');
  }
};

L.init();
window.addEventListener('biogate:locale', renderPasswordToggle);
if (new URLSearchParams(location.search).get('reason') === 'session_expired') showError('SESSION_EXPIRED');
