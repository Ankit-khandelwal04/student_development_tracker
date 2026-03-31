import axios from 'axios';
import AsyncStorage from '@react-native-async-storage/async-storage';
import { Platform } from 'react-native';

// ─────────────────────────────────────────────────────────
// API URL CONFIGURATION
// ─────────────────────────────────────────────────────────
// To override, create mobile/.env.local and set:
//   EXPO_PUBLIC_API_URL=http://YOUR_MACHINE_IP:8000/api
//
// Your machine's current IPs (run ipconfig to verify):
//   Wi-Fi  : 192.168.1.244
//   Ethernet: 192.168.0.64
// ─────────────────────────────────────────────────────────

const HOST_LAN_IP = '192.168.1.244'; // ← UPDATE this whenever your Wi-Fi IP changes (run ipconfig to check)

const getApiUrl = () => {
  // Hard override from environment (highest priority)
  if (typeof process !== 'undefined' && process.env && process.env.EXPO_PUBLIC_API_URL) {
    return process.env.EXPO_PUBLIC_API_URL;
  }

  if (__DEV__) {
    if (Platform.OS === 'android') {
      // Use the host machine's LAN IP — works for both emulator (Hyper-V/WHPX)
      // and physical devices on the same network.
      // NOTE: 10.0.2.2 only works with the classic QEMU Android emulator on
      //       Windows without Hyper-V. Use LAN IP for broad compatibility.
      return `http://${HOST_LAN_IP}:8000/api`;
    } else if (Platform.OS === 'web') {
      return 'http://localhost:8000/api';
    } else {
      // iOS simulator
      return `http://${HOST_LAN_IP}:8000/api`;
    }
  }
  // Production
  return `http://${HOST_LAN_IP}:8000/api`;
};

const API_BASE_URL = getApiUrl();

console.log('API Base URL:', API_BASE_URL);

const apiClient = axios.create({
  baseURL: API_BASE_URL,
  headers: {
    'Content-Type': 'application/json',
  },
  timeout: 10000, // 10 second timeout
});

// Callback to trigger sign-out — set by App.js on startup
let _signOutCallback = null;
export const setSignOutCallback = (fn) => {
  _signOutCallback = fn;
};

// Add token to requests
apiClient.interceptors.request.use(
  async (config) => {
    const token = await AsyncStorage.getItem('userToken');
    if (token) {
      config.headers.Authorization = `Bearer ${token}`;
    }
    return config;
  },
  (error) => {
    return Promise.reject(error);
  }
);

// Add response interceptor for error handling and 401 auto-logout
apiClient.interceptors.response.use(
  (response) => response,
  async (error) => {
    if (error.response) {
      const status = error.response.status;

      if (status === 401) {
        // Token expired or invalid — clear storage and redirect to login
        try {
          await AsyncStorage.removeItem('userToken');
          await AsyncStorage.removeItem('userRole');
          await AsyncStorage.removeItem('apaarId');
        } catch (_) {}

        if (_signOutCallback) {
          _signOutCallback();
        }
      }

      console.error('API Error Response:', status, error.response.data);
    } else if (error.request) {
      // Request made but no response
      console.error('API No Response:', error.request);
      error.message = 'Cannot connect to server. Make sure backend is running.';
    } else {
      console.error('API Error:', error.message);
    }
    return Promise.reject(error);
  }
);

export const authAPI = {
  login: (email, password) =>
    apiClient.post('/auth/login', { email, password }),

  signup: (userData) =>
    apiClient.post('/auth/signup', userData),
};

export const studentAPI = {
  getProfile: () =>
    apiClient.get('/student/profile'),

  getPendingTests: () =>
    apiClient.get('/student/pending-tests'),

  generateEQTest: () =>
    apiClient.post('/student/generate-eq-test', {}, {
      timeout: 30000, // 30 seconds for test generation (LLM can be slow)
    }),

  generateIQTest: () =>
    apiClient.post('/student/generate-iq-test', {}, {
      timeout: 30000, // 30 seconds for test generation (LLM can be slow)
    }),

  submitTest: (testData) =>
    apiClient.post('/student/submit-test', testData, {
      timeout: 30000, // 30 seconds for test submission
    }),

  getQuizHistory: () =>
    apiClient.get('/student/quiz-history'),

  getPhysicalHealth: () =>
    apiClient.get('/student/physical-health', {
      timeout: 30000, // 30 seconds for nutrition plan generation
    }),
};

export const teacherAPI = {
  getStudents: () =>
    apiClient.get('/teacher/students'),

  uploadPhysicalTest: (data) =>
    apiClient.post('/teacher/upload-physical-test', data, {
      timeout: 30000,
    }),

  getPhysicalTest: (apaarId) =>
    apiClient.get(`/teacher/physical-test/${apaarId}`),

  uploadPhysicalTestsBulk: (items) =>
    apiClient.post('/teacher/upload-physical-tests-bulk', { items }, {
      timeout: 60000,
    }),

  getStudentQuizHistory: (apaarId) =>
    apiClient.get('/teacher/student-quiz-history', { params: { apaar_id: apaarId } }),
};


export const parentAPI = {
  getChildProfile: () =>
    apiClient.get('/parent/child-profile'),

  getTestResults: () =>
    apiClient.get('/parent/test-results'),

  getQuizHistory: () =>
    apiClient.get('/parent/quiz-history'),

  generateReport: () =>
    apiClient.post('/parent/generate-report', {}, {
      timeout: 60000, // 60 seconds for report generation (LLM can be slow)
    }),

  getRemedies: () =>
    apiClient.get('/parent/remedies'),

  checkSymptoms: (symptoms, additionalNotes = '') =>
    apiClient.post('/parent/symptom-check', {
      symptoms,
      additional_notes: additionalNotes || undefined,
    }, {
      timeout: 30000, // 30s — Gemini LLM call
    }),
};


export default apiClient;
