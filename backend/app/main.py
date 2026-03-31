import asyncio
from fastapi import FastAPI, HTTPException, Depends, status, Request, BackgroundTasks
from fastapi.exceptions import RequestValidationError
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from datetime import timedelta, datetime, timezone
from typing import List, Optional
from pydantic import BaseModel
from app.config import settings
from app.database import (
    users_collection,
    student_profiles_collection,
    test_results_collection,
    actionable_remedies_collection,
    quiz_history_collection,
    create_indexes,
)
from app.models import (
    User, UserCreate, UserLogin, Token, StudentProfile,
    TestResult, ActionableRemedy, PhysicalTestInput, PhysicalBulkUploadRequest, UserRole, TestType
)
from app.auth import get_password_hash, verify_password, create_access_token, decode_access_token
from app.llm_service import (
    generate_eq_test,
    generate_iq_test,
    generate_parent_report,
    generate_quiz_report_and_remedies,
    generate_physical_advice,
)
from fastapi.responses import JSONResponse
import logging

app = FastAPI(title="Student Development Tracking API")

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBearer()

# Helper function to get Indian Standard Time
def get_ist_now():
    """Get current time in Indian Standard Time (IST = UTC+5:30)"""
    ist_offset = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist_offset)

# Background task for generating parent report
async def generate_parent_report_background(apaar_id: str, email: str):
    """Background task to generate comprehensive parent report after test submission"""
    try:
        logger.info(f"[BACKGROUND TASK] Starting parent report generation for {apaar_id}")
        
        # Fetch student profile
        profile = await student_profiles_collection.find_one({"apaar_id": apaar_id})
        if not profile:
            profile = {"full_name": "Student", "grade": None}
            logger.warning(f"[BACKGROUND TASK] No profile found for {apaar_id}, using default")
        else:
            logger.info(f"[BACKGROUND TASK] Found profile for {apaar_id}")
        
        # Fetch all test results for this student
        all_results = await test_results_collection.find(
            {"apaar_id": apaar_id}
        ).to_list(length=100)
        logger.info(f"[BACKGROUND TASK] Found {len(all_results)} test results for {apaar_id}")
        
        if len(all_results) == 0:
            logger.warning(f"[BACKGROUND TASK] No test results found for {apaar_id}, skipping report generation")
            return
        
        # Generate comprehensive parent report with chart data
        logger.info(f"[BACKGROUND TASK] Calling generate_parent_report for {apaar_id}")
        parent_report = await generate_parent_report(
            apaar_id=apaar_id,
            test_results=all_results,
            student_profile=profile
        )
        
        # Log report type and details
        if parent_report.get("is_fallback"):
            logger.warning(f"[BACKGROUND TASK] ⚠️  FALLBACK report generated for {apaar_id}")
            logger.warning(f"[BACKGROUND TASK] Reason: {parent_report.get('fallback_reason', 'Unknown')}")
        else:
            logger.info(f"[BACKGROUND TASK] ✅ REAL LLM report generated for {apaar_id}")
        
        logger.info(f"[BACKGROUND TASK] Report contains {len(parent_report.get('visuals', []))} chart(s)")
        logger.info(f"[BACKGROUND TASK] Report contains {len(parent_report.get('Targeted_SEL_Activities', []))} SEL activities")
        
        # Store the report in actionable_remedies collection
        report_doc = {
            "apaar_id": apaar_id,
            "generated_at": get_ist_now(),
            "data_analysis": parent_report.get("Data_Analysis", ""),
            "sub_grouping_recommendation": parent_report.get("Sub_grouping_Recommendation", ""),
            "targeted_sel_activities": parent_report.get("Targeted_SEL_Activities", []),
            "progress_tracking": parent_report.get("Progress_Tracking", ""),
            "visuals": parent_report.get("visuals", []),
            "competency_scores": {},
            "created_at": get_ist_now(),
        }
        
        remedy_insert = await actionable_remedies_collection.insert_one(report_doc)
        logger.info(f"[BACKGROUND TASK] Stored report in actionable_remedies for {apaar_id}, ID: {remedy_insert.inserted_id}")
        
        # Also add to quiz_history for tracking
        child_user = await users_collection.find_one({"role": "student", "apaar_id": apaar_id})
        child_email = child_user.get("email") if child_user else email
        
        await quiz_history_collection.insert_one({
            "kind": "parent_report",
            "child_email": child_email,
            "apaar_id": apaar_id,
            "actionable_remedy_id": str(remedy_insert.inserted_id),
            "included_test_result_ids": [str(r.get("_id")) for r in all_results if r.get("_id")],
            "report": {
                "Data_Analysis": parent_report.get("Data_Analysis", ""),
                "Sub_grouping_Recommendation": parent_report.get("Sub_grouping_Recommendation", ""),
                "Targeted_SEL_Activities": parent_report.get("Targeted_SEL_Activities", []),
                "Progress_Tracking": parent_report.get("Progress_Tracking", ""),
                "visuals": parent_report.get("visuals", []),
            },
            "created_at": get_ist_now(),
        })
        logger.info(f"[BACKGROUND TASK] Stored report in quiz_history for {apaar_id}")
        
        logger.info(f"[BACKGROUND TASK] ✅ Successfully completed parent report generation for {apaar_id}")
        
    except Exception as e:
        # Log but don't fail - this is a background task
        logger.exception(f"[BACKGROUND TASK] ❌ Failed to generate parent report for {apaar_id}: {e}")

@app.on_event("startup")
async def startup_event():
    await create_indexes()

logger = logging.getLogger("uvicorn.error")

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    body_bytes = await request.body()
    body_text = body_bytes.decode("utf-8", errors="replace")
    logger.error("Validation error on %s\nBody: %s\nErrors: %s",
                 request.url.path, body_text, exc.errors())
    # Return the normal 422 but include the body for debugging
    return JSONResponse(status_code=422, content={"detail": exc.errors(), "body": body_text})

# Authentication dependency
async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    payload = decode_access_token(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid authentication credentials")
    
    email = payload.get("sub")
    user = await users_collection.find_one({"email": email})
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    
    return user

# Auth endpoints
@app.post("/api/auth/signup", response_model=Token)
async def signup(user_data: UserCreate):
    try:
        existing_user = await users_collection.find_one({"email": user_data.email})
        if existing_user:
            raise HTTPException(status_code=400, detail="Email already registered")
        
        # Normalize apaar_id: strip all whitespace so '1234 5678 9876' == '123456789876'
        normalized_apaar_id = None
        if user_data.apaar_id:
            normalized_apaar_id = "".join(str(user_data.apaar_id).split()).upper()
        
        # Create user document
        user_dict = {
            "email": user_data.email,
            "hashed_password": get_password_hash(user_data.password),
            "role": user_data.role.value if isinstance(user_data.role, UserRole) else user_data.role,
            "full_name": user_data.full_name,
            "apaar_id": normalized_apaar_id,
            "created_at": get_ist_now()
        }
        
        await users_collection.insert_one(user_dict)
        
        access_token = create_access_token(
            data={"sub": user_data.email, "role": user_data.role.value if isinstance(user_data.role, UserRole) else user_data.role},
            expires_delta=timedelta(minutes=settings.access_token_expire_minutes)
        )
        
        return Token(
            access_token=access_token,
            token_type="bearer",
            role=user_data.role,
            apaar_id=normalized_apaar_id
        )
    except HTTPException:
        raise
    except Exception as e:
        print(f"Signup error: {e}")
        raise HTTPException(status_code=500, detail=f"Signup failed: {str(e)}")

@app.post("/api/auth/login", response_model=Token)
async def login(user_data: UserLogin):
    user = await users_collection.find_one({"email": user_data.email})
    if not user or not verify_password(user_data.password, user.get("hashed_password", "")):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    
    user_role = user["role"]
    access_token = create_access_token(
        data={"sub": user_data.email, "role": user_role},
        expires_delta=timedelta(minutes=settings.access_token_expire_minutes)
    )
    
    return Token(
        access_token=access_token,
        token_type="bearer",
        role=user_role,
        apaar_id=user.get("apaar_id")
    )

# Student endpoints
@app.get("/api/student/profile")
async def get_student_profile(current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "student" and current_user["role"] != "parent":
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Determine which user to look up: for parent, look up their child by apaar_id
    if current_user["role"] == "parent":
        target_apaar_id = current_user.get("apaar_id")
        if not target_apaar_id:
            raise HTTPException(status_code=404, detail="No child APAAR ID linked to this parent account")
        profile = await users_collection.find_one({"apaar_id": target_apaar_id, "role": "student"})
        if not profile:
            # Fallback: return the parent's own profile if no child found
            profile = await users_collection.find_one({"email": current_user["email"]})
    else:
        profile = await users_collection.find_one({"email": current_user["email"]})
    
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    
    profile["_id"] = str(profile["_id"])
    profile.pop("hashed_password", None)  # Never expose password hash
    
    # Merge student_profiles data (grade, school, DOB, etc.) into the response
    apaar_id = profile.get("apaar_id")
    if apaar_id:
        student_profile_doc = await student_profiles_collection.find_one({"apaar_id": apaar_id})
        if student_profile_doc:
            student_profile_doc.pop("_id", None)
            # Merge: student_profiles fields supplement but don't overwrite user fields
            for key, value in student_profile_doc.items():
                if key not in profile:
                    profile[key] = value
    
    return profile

@app.get("/api/student/pending-tests")
async def get_pending_tests(current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "student" and current_user["role"] != "parent":
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Return pending test types (simplified logic)
    return {"pending_tests": ["eq", "iq"]}

@app.post("/api/student/generate-eq-test")
async def get_eq_test(current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "student" and current_user["role"] != "parent":
        raise HTTPException(status_code=403, detail="Access denied")

    profile = await student_profiles_collection.find_one({"apaar_id": current_user["apaar_id"]})
    grade = profile.get("grade", 5) if profile else 5

    try:
        # ✅ 60s hard timeout so the route never hangs and blocks other requests
        questions = await asyncio.wait_for(
            generate_eq_test(grade),
            timeout=60.0
        )
    except asyncio.TimeoutError:
        logger.warning("generate-eq-test timed out after 60s")
        raise HTTPException(
            status_code=503,
            detail="Test generation timed out. Please try again.",
        )
    except Exception as e:
        err_name = type(e).__name__
        err_msg = str(e).lower()
        if err_name == "RateLimitError" or "429" in err_msg or "rate" in err_msg or "too_many_requests" in err_msg or "high traffic" in err_msg:
            raise HTTPException(
                status_code=503,
                detail="Service is busy. Please try again in a moment.",
            )
        logger.exception("generate-eq-test failed")
        raise HTTPException(
            status_code=502,
            detail="Could not generate test. Please try again.",
        )

    if not questions:
        raise HTTPException(
            status_code=502,
            detail="No questions were generated. Please try again.",
        )

    return {"questions": questions}


@app.post("/api/student/generate-iq-test")
async def get_iq_test(current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "student" and current_user["role"] != "parent":
        raise HTTPException(status_code=403, detail="Access denied")

    profile = await student_profiles_collection.find_one({"apaar_id": current_user["apaar_id"]})
    grade = profile.get("grade", 5) if profile else 5

    # For now, IQ questions are generated from a static helper and
    # are unlikely to fail; keep error handling simple.
    try:
        questions = await generate_iq_test(grade)
    except Exception:
        logger.exception("generate-iq-test failed")
        raise HTTPException(
            status_code=502,
            detail="Could not generate test. Please try again.",
        )
    return {"questions": questions}

@app.post("/api/student/submit-test")
async def submit_test(test_result: TestResult, background_tasks: BackgroundTasks, current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "student" and current_user["role"] != "parent":
        raise HTTPException(status_code=403, detail="Access denied")
    
    test_result.apaar_id = current_user["apaar_id"]
    result_dict = test_result.dict()
    # Convert enum to string
    if "test_type" in result_dict and hasattr(result_dict["test_type"], "value"):
        result_dict["test_type"] = result_dict["test_type"].value

    # Compute IQ score (percentage correct) if applicable
    if result_dict.get("test_type") == "iq" and result_dict.get("questions") and result_dict.get("answers"):
        questions = result_dict.get("questions") or []
        answers = result_dict.get("answers") or []
        total = 0
        correct = 0
        for idx, q in enumerate(questions):
            if idx >= len(answers):
                break
            a = answers[idx]
            try:
                # Accept numeric or numeric-like string
                if isinstance(a, str):
                    a_val = int(a)
                else:
                    a_val = int(a)
            except Exception:
                continue
            if isinstance(q, dict) and isinstance(q.get("correct_index"), int):
                total += 1
                if a_val == q["correct_index"]:
                    correct += 1
        if total > 0:
            result_dict["score"] = round((correct / total) * 100.0, 1)

    insert_result = await test_results_collection.insert_one(result_dict)

    profile = await student_profiles_collection.find_one({"apaar_id": current_user["apaar_id"]})
    if not profile:
        profile = {"full_name": current_user.get("full_name", "Student"), "grade": None}

    quiz_report = None
    if result_dict.get("test_type") in ("eq", "iq") and result_dict.get("questions"):
        try:
            quiz_report = await generate_quiz_report_and_remedies(
                result_dict.get("questions") or [],
                result_dict.get("answers") or [],
                result_dict.get("score"),
                profile,
            )
            
            # Log quiz report details
            if quiz_report:
                if quiz_report.get("is_fallback"):
                    logger.warning(f"⚠️  FALLBACK quiz report for {current_user['apaar_id']}")
                    logger.warning(f"Reason: {quiz_report.get('fallback_reason', 'Unknown')}")
                else:
                    logger.info(f"✅ REAL LLM quiz report for {current_user['apaar_id']}")
                logger.info(f"Quiz report: {len(quiz_report.get('visuals', []))} chart(s), {len(quiz_report.get('Targeted_SEL_Activities', []))} activities")
        except Exception as e:
            logger.exception("Per-quiz report generation failed: %s", e)
            quiz_report = None

    history_doc = {
        "kind": "quiz_attempt",
        "child_email": current_user["email"],
        "apaar_id": current_user["apaar_id"],
        "test_result_id": str(insert_result.inserted_id),
        "test_type": result_dict.get("test_type"),
        "test_date": result_dict.get("test_date"),
        "questions": result_dict.get("questions"),
        "answers": result_dict.get("answers"),
        "score": result_dict.get("score"),
        "created_at": get_ist_now(),
        "report": quiz_report,
    }
    history_insert = await quiz_history_collection.insert_one(history_doc)

    # If LLM failed before insert, try again with update
    if not quiz_report and result_dict.get("questions"):
        try:
            quiz_report = await generate_quiz_report_and_remedies(
                result_dict.get("questions") or [],
                result_dict.get("answers") or [],
                result_dict.get("score"),
                profile,
            )
            if quiz_report:
                await quiz_history_collection.update_one(
                    {"_id": history_insert.inserted_id},
                    {"$set": {"report": quiz_report}},
                )
                # Log retry success
                if quiz_report.get("is_fallback"):
                    logger.warning(f"⚠️  FALLBACK quiz report (retry) for {current_user['apaar_id']}")
                else:
                    logger.info(f"✅ REAL LLM quiz report (retry) for {current_user['apaar_id']}")
        except Exception as e:
            logger.exception("Per-quiz report retry failed: %s", e)

    # Schedule background task to auto-generate comprehensive parent report
    # This runs asynchronously and doesn't block the response
    background_tasks.add_task(
        generate_parent_report_background,
        current_user["apaar_id"],
        current_user["email"]
    )
    logger.info(f"Scheduled background parent report generation for {current_user['apaar_id']}")

    return {"message": "Test submitted successfully"}

@app.get("/api/student/quiz-history")
async def get_student_quiz_history(current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "student" and current_user["role"] != "parent":
        raise HTTPException(status_code=403, detail="Access denied")
    
    history = []
    # Only return quiz_attempt entries, not parent_report entries
    async for item in quiz_history_collection.find(
        {"child_email": current_user["email"], "kind": "quiz_attempt"}
    ).sort("created_at", -1):
        item["_id"] = str(item["_id"])
        history.append(item)
    return {"quiz_history": history}

@app.get("/api/student/physical-health")
async def get_student_physical_health(current_user: dict = Depends(get_current_user)):
    """Get student's physical metrics and generate personalized nutrition plan"""
    if current_user["role"] != "student" and current_user["role"] != "parent":
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Fetch latest physical test data
    physical_test = await test_results_collection.find_one(
        {"apaar_id": current_user["apaar_id"], "test_type": "physical"},
        sort=[("created_at", -1)]
    )
    
    if not physical_test:
        return {"has_data": False, "physical_metrics": None, "nutrition_plan": None}
    
    # Fetch student profile for age/gender
    profile = await student_profiles_collection.find_one({"apaar_id": current_user["apaar_id"]})
    if not profile:
        profile = await users_collection.find_one({"apaar_id": current_user["apaar_id"]})
    
    # Generate nutrition plan
    from app.llm_service import generate_nutrition_plan
    try:
        nutrition_plan = await generate_nutrition_plan(
            physical_metrics=physical_test.get("physical_metrics", {}),
            student_profile=profile or {},
            health_notes=physical_test.get("notes", "")
        )
    except Exception as e:
        logger.exception(f"Failed to generate nutrition plan: {e}")
        nutrition_plan = None
    
    return {
        "has_data": True,
        "physical_metrics": physical_test.get("physical_metrics", {}),
        "health_notes": physical_test.get("notes", ""),
        "last_updated": physical_test.get("created_at"),
        "nutrition_plan": nutrition_plan
    }

# Teacher endpoints
@app.get("/api/teacher/students")
async def get_students(current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "teacher":
        raise HTTPException(status_code=403, detail="Access denied")
    
    students = []
    async for student in users_collection.find({"role": "student"}):
        student["_id"] = str(student["_id"])
        # Remove sensitive fields before returning
        student.pop("hashed_password", None)
        # Merge student_profiles data for grade, school info
        apaar_id = student.get("apaar_id")
        if apaar_id:
            sp = await student_profiles_collection.find_one({"apaar_id": apaar_id})
            if sp:
                sp.pop("_id", None)
                for key, value in sp.items():
                    if key not in student:
                        student[key] = value
        students.append(student)
    
    return {"students": students}

@app.post("/api/teacher/upload-physical-test")
async def upload_physical_test(
    data: PhysicalTestInput,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user)
):
    if current_user["role"] != "teacher":
        raise HTTPException(status_code=403, detail="Access denied")

    # Normalize APAAR ID — strip spaces so '1234 5678 9875' == '123456789875'
    raw_apaar = data.apaar_id or ""
    normalized_apaar_id = "".join(raw_apaar.split()).upper()
    if not normalized_apaar_id:
        raise HTTPException(status_code=400, detail="apaar_id is required")

    logger.info("=" * 60)
    logger.info("PHYSICAL TEST DATA RECEIVED for APAAR: %s", normalized_apaar_id)
    logger.info("=" * 60)

    # Build physical_metrics dict, stripping None values
    physical_metrics = {
        "bmi": data.bmi,
        "fitness_score": data.fitness_score,
        "height_cm": data.height_cm,
        "weight_kg": data.weight_kg,
        "resting_heart_rate": data.resting_heart_rate,
        "systolic_bp": data.systolic_bp,
        "diastolic_bp": data.diastolic_bp,
        "sleep_hours": data.sleep_hours,
        "additional_metrics": data.additional_metrics,
    }
    physical_metrics = {k: v for k, v in physical_metrics.items() if v is not None}

    test_result_dict = {
        "apaar_id": normalized_apaar_id,
        "test_type": "physical",
        "test_date": get_ist_now(),
        "physical_metrics": physical_metrics,
        "notes": data.health_notes,
        "physical_advice": None,   # Will be filled by background task
        "created_at": get_ist_now()
    }

    insert_result = await test_results_collection.insert_one(test_result_dict)
    inserted_id = insert_result.inserted_id

    # Generate physical advice in the background so we don't timeout the request
    async def generate_advice_background(doc_id, metrics, apaar_id, health_notes):
        try:
            student_profile = await student_profiles_collection.find_one({"apaar_id": apaar_id}) or {}
            advice = await generate_physical_advice(metrics, student_profile, health_notes)
            if advice:
                await test_results_collection.update_one(
                    {"_id": doc_id},
                    {"$set": {"physical_advice": advice}}
                )
                logger.info("Physical advice saved for %s", apaar_id)
        except Exception:
            logger.exception("Background physical advice generation failed for %s", apaar_id)

    background_tasks.add_task(
        generate_advice_background,
        inserted_id,
        physical_metrics,
        normalized_apaar_id,
        data.health_notes
    )

    return {
        "message": "Physical test data uploaded successfully. AI advice is being generated in the background.",
        "apaar_id": normalized_apaar_id
    }

@app.get("/api/teacher/physical-test/{apaar_id:path}")
async def get_physical_test(apaar_id: str, current_user: dict = Depends(get_current_user)):
    """Get the latest physical test data for a student with nutrition plan"""
    if current_user["role"] != "teacher":
        raise HTTPException(status_code=403, detail="Access denied")

    # Normalize APAAR ID — strip spaces & uppercase to match stored format
    normalized_apaar_id = "".join(apaar_id.split()).upper()

    # Find the most recent physical test for this student
    physical_test = await test_results_collection.find_one(
        {"apaar_id": normalized_apaar_id, "test_type": "physical"},
        sort=[("created_at", -1)]
    )
    
    if not physical_test:
        return {"has_data": False, "data": None}
    
    # Convert ObjectId to string
    physical_test["_id"] = str(physical_test["_id"])
    
    # Fetch student profile for nutrition plan generation
    profile = await student_profiles_collection.find_one({"apaar_id": apaar_id})
    
    # Generate nutrition plan
    from app.llm_service import generate_nutrition_plan
    try:
        nutrition_plan = await generate_nutrition_plan(
            physical_metrics=physical_test.get("physical_metrics", {}),
            student_profile=profile or {},
            health_notes=physical_test.get("notes")
        )
        physical_test["nutrition_plan"] = nutrition_plan
    except Exception as e:
        logger.exception(f"Failed to generate nutrition plan: {e}")
        physical_test["nutrition_plan"] = None
    
    return {"has_data": True, "data": physical_test}

@app.post("/api/teacher/upload-physical-tests-bulk")
async def upload_physical_tests_bulk(payload: PhysicalBulkUploadRequest, current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "teacher":
        raise HTTPException(status_code=403, detail="Access denied")

    inserted = 0
    failed = 0
    errors = []

    for idx, item in enumerate(payload.items or []):
        try:
            email = item.email
            apaar_id = item.apaar_id
            if not email and not apaar_id:
                failed += 1
                errors.append({"row": idx + 1, "error": "Missing email or apaar_id"})
                continue

            if not apaar_id and email:
                user = await users_collection.find_one({"email": email})
                apaar_id = user.get("apaar_id") if user else None
                if not apaar_id:
                    failed += 1
                    errors.append({"row": idx + 1, "error": f"No student found for email {email}"})
                    continue

            physical_metrics = {
                "bmi": item.bmi,
                "fitness_score": item.fitness_score,
                "height_cm": item.height_cm,
                "weight_kg": item.weight_kg,
                "resting_heart_rate": item.resting_heart_rate,
                "systolic_bp": item.systolic_bp,
                "diastolic_bp": item.diastolic_bp,
                "sleep_hours": item.sleep_hours,
                "additional_metrics": item.additional_metrics,
            }
            physical_metrics = {k: v for k, v in physical_metrics.items() if v is not None}

            student_profile = await student_profiles_collection.find_one({"apaar_id": apaar_id}) or {}
            advice = None
            try:
                advice = await generate_physical_advice(physical_metrics, student_profile, item.health_notes)
            except Exception:
                logger.exception("generate_physical_advice failed in bulk")
                advice = None

            test_result_dict = {
                "apaar_id": apaar_id,
                "test_type": "physical",
                "test_date": get_ist_now(),
                "physical_metrics": physical_metrics,
                "notes": item.health_notes,
                "physical_advice": advice,
                "created_at": get_ist_now(),
            }
            await test_results_collection.insert_one(test_result_dict)
            inserted += 1
        except Exception as e:
            failed += 1
            errors.append({"row": idx + 1, "error": str(e)})

    return {"inserted": inserted, "failed": failed, "errors": errors[:50]}

# Parent endpoints
@app.get("/api/parent/child-profile")
async def get_child_profile(current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "parent":
        raise HTTPException(status_code=403, detail="Access denied")
    
    raw_apaar_id = current_user.get("apaar_id")
    # Normalize the stored apaar_id (strip spaces) for robust lookup
    target_apaar_id = "".join(str(raw_apaar_id).split()).upper() if raw_apaar_id else None
    
    if not target_apaar_id:
        # Parent has no child linked — return a soft empty response instead of crashing
        return {"child_linked": False, "message": "No child APAAR ID linked to this parent account"}
    
    # Find child by normalized apaar_id
    profile = await users_collection.find_one({"apaar_id": target_apaar_id, "role": "student"})
    if not profile:
        # Child not registered yet — return soft response so frontend can show a helpful message
        return {
            "child_linked": True,
            "child_registered": False,
            "apaar_id": target_apaar_id,
            "message": f"No student account found with APAAR ID {target_apaar_id}. Ask your child to register first."
        }
    
    profile["_id"] = str(profile["_id"])
    profile.pop("hashed_password", None)  # Never expose password hash
    profile["child_linked"] = True
    profile["child_registered"] = True
    
    # Merge full student_profiles data (grade, school, DOB, etc.)
    student_profile_doc = await student_profiles_collection.find_one({"apaar_id": target_apaar_id})
    if student_profile_doc:
        student_profile_doc.pop("_id", None)
        for key, value in student_profile_doc.items():
            if key not in profile:
                profile[key] = value
    
    return profile

@app.get("/api/parent/test-results")
async def get_child_test_results(current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "parent":
        raise HTTPException(status_code=403, detail="Access denied")

    # Normalize apaar_id for lookup
    raw_apaar_id = current_user.get("apaar_id")
    normalized_apaar_id = "".join(str(raw_apaar_id).split()).upper() if raw_apaar_id else None
    if not normalized_apaar_id:
        return {"test_results": []}

    results = []

    # 1. Physical test results from test_results_collection (keyed by apaar_id)
    async for result in test_results_collection.find({"apaar_id": normalized_apaar_id}):
        result["_id"] = str(result["_id"])
        results.append(result)

    # 2. EQ/IQ quiz results from quiz_history_collection (keyed by apaar_id or child_email)
    #    Find the child's email first
    child_user = await users_collection.find_one({"apaar_id": normalized_apaar_id, "role": "student"})
    if child_user:
        child_email = child_user.get("email")
        async for item in quiz_history_collection.find(
            {"apaar_id": normalized_apaar_id, "kind": "quiz_attempt"}
        ).sort("created_at", -1):
            item["_id"] = str(item["_id"])
            results.append(item)

        # Also capture any quiz results stored only by email (backward compat)
        if child_email:
            async for item in quiz_history_collection.find(
                {"child_email": child_email, "kind": "quiz_attempt",
                 "apaar_id": {"$ne": normalized_apaar_id}}  # avoid duplicates
            ).sort("created_at", -1):
                item["_id"] = str(item["_id"])
                results.append(item)

    # Sort combined results by date descending
    results.sort(key=lambda r: str(r.get("created_at") or r.get("test_date") or ""), reverse=True)

    return {"test_results": results}


# ─────────────────────────────────────────────────────────────────────────────
# SYMPTOM CHECKER — Preventive Suggestions for Parents
# ─────────────────────────────────────────────────────────────────────────────

class SymptomCheckRequest(BaseModel):
    symptoms: List[str]  # e.g. ["stress", "fatigue", "low appetite"]
    additional_notes: Optional[str] = None


RULE_BASED_SUGGESTIONS = {
    "stress": {
        "home": ["Encourage creative activities (drawing, music, crafts)", "Ensure a consistent bedtime routine"],
        "emotional": ["Practice 5-minute breathing exercises together", "Have a calm, open daily check-in conversation"]
    },
    "fatigue": {
        "home": ["Ensure 9–11 hours of sleep for school-age children", "Reduce screen time 1 hour before bed"],
        "emotional": ["Check if academic or social pressures are causing exhaustion"]
    },
    "anxiety": {
        "home": ["Limit caffeine (chocolate, soda)", "Establish predictable daily routines"],
        "emotional": ["Validate feelings without minimizing them", "Practice grounding exercises (name 5 things you can see)"]
    },
    "low appetite": {
        "home": ["Offer small frequent meals rather than large ones", "Make mealtimes calm and screen-free"],
        "emotional": ["Check if anxiety or social issues around lunchtime at school are affecting appetite"]
    },
    "headache": {
        "home": ["Ensure adequate hydration throughout the day", "Check if the child needs an eye test"],
        "emotional": []
    },
    "irritability": {
        "home": ["Check sleep quality and iron/vitamin D levels"],
        "emotional": ["Spend 15 uninterrupted minutes daily with child in their chosen activity"]
    },
    "difficulty concentrating": {
        "home": ["Ensure a clutter-free, quiet study space", "Try the Pomodoro technique: 25 min study, 5 min break"],
        "emotional": ["Discuss if the subject matter is confusing — arrange extra help early"]
    },
    "social withdrawal": {
        "home": ["Arrange small playdates rather than large groups"],
        "emotional": ["Gently ask about friendships, listen without judgment", "Consult school counselor if withdrawal persists > 2 weeks"]
    },
    "sadness": {
        "home": ["Ensure regular physical activity and natural sunlight"],
        "emotional": ["Create a 'feelings journal' for the child", "Seek professional guidance if sadness persists > 2 weeks"]
    },
    "stomach pain": {
        "home": ["Check diet for potential food sensitivities", "Ensure regular mealtimes"],
        "emotional": ["Unexplained stomach pain is often linked to anxiety — explore school-related worries"]
    },
}

HIGH_URGENCY_SYMPTOMS = {"sadness", "social withdrawal", "anxiety", "self-harm", "aggression"}


async def _generate_symptom_suggestions_llm(symptoms: List[str], notes: Optional[str], child_profile: dict) -> dict:
    """Use Gemini to generate personalised suggestions. Rotates between key 1 and key 2."""
    from google import genai
    from google.genai import types
    from app.llm_service import _get_gemini_api_key
    import json

    child_name = child_profile.get("full_name", "the child")
    grade = child_profile.get("grade", "")
    grade_text = f" in grade {grade}" if grade else ""
    symptom_list = ", ".join(symptoms)
    notes_text = f"Additional parent notes: {notes}" if notes else ""

    prompt = f"""You are a child wellness advisor helping a parent.

Child: {child_name}{grade_text}
Reported symptoms: {symptom_list}
{notes_text}

Provide:
1. home_remedies: 3 practical home/lifestyle suggestions (list)
2. emotional_support: 2-3 emotional support strategies for parents (list)
3. consult_professional: true if symptoms suggest professional evaluation needed (boolean)
4. professional_note: brief note on what kind of professional to consult and why (string, or "" if not needed)
5. urgency: "low", "medium", or "high" based on severity
6. summary: one-sentence compassionate summary for the parent

Respond ONLY with valid JSON matching this exact schema:
{{
  "home_remedies": ["..."],
  "emotional_support": ["..."],
  "consult_professional": false,
  "professional_note": "",
  "urgency": "low",
  "summary": "..."
}}"""

    last_err = None
    for key_index in range(2):          # Try key 1 then key 2
        try:
            api_key = _get_gemini_api_key(key_index)
            client_genai = genai.Client(api_key=api_key)
            response = client_genai.models.generate_content(
                model="gemini-1.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                ),
            )
            raw = response.text.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            return json.loads(raw)
        except Exception as e:
            last_err = e
            logger.warning("Gemini key %d failed for symptom check: %s", key_index + 1, str(e))

    raise last_err


def _generate_symptom_suggestions_fallback(symptoms: List[str]) -> dict:
    """Rule-based fallback suggestions when LLM fails."""
    home = []
    emotional = []
    consult = False

    lowered = [s.lower() for s in symptoms]

    for s in lowered:
        rules = RULE_BASED_SUGGESTIONS.get(s, {})
        home.extend(rules.get("home", []))
        emotional.extend(rules.get("emotional", []))
        if s in HIGH_URGENCY_SYMPTOMS:
            consult = True

    # Deduplicate and cap
    home = list(dict.fromkeys(home))[:4]
    emotional = list(dict.fromkeys(emotional))[:3]

    if not home:
        home = ["Maintain a consistent daily routine", "Ensure adequate sleep and balanced nutrition", "Encourage physical activity"]
    if not emotional:
        emotional = ["Have open, non-judgmental conversations", "Spend quality time together daily"]

    urgency = "high" if consult else ("medium" if len(symptoms) >= 3 else "low")
    professional_note = "Consider consulting a pediatrician or child psychologist given the reported symptoms." if consult else ""

    return {
        "home_remedies": home,
        "emotional_support": emotional,
        "consult_professional": consult,
        "professional_note": professional_note,
        "urgency": urgency,
        "summary": f"Based on {len(symptoms)} reported symptom(s), here are recommended actions to support your child."
    }


@app.post("/api/parent/symptom-check")
async def symptom_check(
    request: SymptomCheckRequest,
    current_user: dict = Depends(get_current_user)
):
    if current_user["role"] != "parent":
        raise HTTPException(status_code=403, detail="Access denied")

    if not request.symptoms:
        raise HTTPException(status_code=400, detail="At least one symptom is required")

    # Get child profile for personalization
    raw_apaar = current_user.get("apaar_id") or ""
    normalized_apaar = "".join(raw_apaar.split()).upper()
    child_profile = {}
    if normalized_apaar:
        child_user = await users_collection.find_one({"apaar_id": normalized_apaar, "role": "student"})
        if child_user:
            child_profile = {
                "full_name": child_user.get("full_name", ""),
                "grade": ""
            }
            sp = await student_profiles_collection.find_one({"apaar_id": normalized_apaar})
            if sp:
                child_profile["grade"] = sp.get("grade", "")

    # Try LLM first, fall back to rule-based
    try:
        result = await _generate_symptom_suggestions_llm(
            request.symptoms, request.additional_notes, child_profile
        )
        result["source"] = "ai"
        logger.info("Symptom check LLM success for %s", current_user.get("email"))
    except Exception as e:
        logger.warning("Symptom check LLM failed (%s), using fallback", str(e))
        result = _generate_symptom_suggestions_fallback(request.symptoms)
        result["source"] = "fallback"

    return result


@app.post("/api/parent/generate-report")
async def generate_report(current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "parent":
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Get student profile
    profile = await student_profiles_collection.find_one({"apaar_id": current_user["apaar_id"]})
    if not profile:
        profile = await users_collection.find_one({"apaar_id": current_user["apaar_id"]})
    if not profile:
        raise HTTPException(status_code=404, detail="Child profile not found")

    try:
        dob = profile.get("date_of_birth")
        if dob:
            now = get_ist_now()
            age_years = now.year - dob.year - ((now.month, now.day) < (dob.month, dob.day))
            profile["age_years"] = max(age_years, 0)
    except Exception:
        pass
    
    results = []
    async for result in test_results_collection.find({"apaar_id": current_user["apaar_id"]}):
        result["_id"] = str(result["_id"])
        results.append(result)
    
    report_data = await generate_parent_report(current_user["apaar_id"], results, profile)
    
    # Log report details
    if report_data.get("is_fallback"):
        logger.warning(f"⚠️  FALLBACK parent report for {current_user['apaar_id']}")
        logger.warning(f"Reason: {report_data.get('fallback_reason', 'Unknown')}")
    else:
        logger.info(f"✅ REAL LLM parent report for {current_user['apaar_id']}")
    logger.info(f"Parent report: {len(report_data.get('visuals', []))} chart(s), {len(report_data.get('Targeted_SEL_Activities', []))} activities")
    
    now = get_ist_now()
    remedy_dict = {
        "apaar_id": current_user["apaar_id"],
        "generated_at": now.isoformat(),           # ✅ convert datetime to string
        "data_analysis": report_data["Data_Analysis"],
        "sub_grouping_recommendation": report_data["Sub_grouping_Recommendation"],
        "targeted_sel_activities": report_data["Targeted_SEL_Activities"],
        "progress_tracking": report_data["Progress_Tracking"],
        "visuals": report_data.get("visuals", []),
        "competency_scores": {},
        "created_at": now.isoformat()              # ✅ convert datetime to string
    }

    # Use a separate dict with native datetime for MongoDB inserts
    remedy_db_dict = {**remedy_dict, "generated_at": now, "created_at": now}
    remedy_insert = await actionable_remedies_collection.insert_one(remedy_db_dict)
    
    child_user = await users_collection.find_one({"role": "student", "apaar_id": current_user["apaar_id"]})
    if not child_user:
        child_user = await users_collection.find_one({"email": current_user["email"]})
    child_email = child_user.get("email") if child_user else None

    await quiz_history_collection.insert_one({
        "kind": "parent_report",
        "child_email": child_email,
        "apaar_id": current_user["apaar_id"],
        "actionable_remedy_id": str(remedy_insert.inserted_id),  # ✅ already stringified
        "included_test_result_ids": [r.get("_id") for r in results if r.get("_id")],
        "report": {
            "Data_Analysis": report_data["Data_Analysis"],
            "Sub_grouping_Recommendation": report_data["Sub_grouping_Recommendation"],
            "Targeted_SEL_Activities": report_data["Targeted_SEL_Activities"],
            "Progress_Tracking": report_data["Progress_Tracking"],
            "visuals": report_data.get("visuals", []),
        },
        "created_at": now,
    })
    
    remedy_dict["id"] = str(remedy_insert.inserted_id)  # ✅ add inserted ID as string
    return remedy_dict

@app.get("/api/parent/remedies")
async def get_remedies(current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "parent":
        raise HTTPException(status_code=403, detail="Access denied")
    
    remedies = []
    async for remedy in actionable_remedies_collection.find({"apaar_id": current_user["apaar_id"]}).sort("created_at", -1):
        remedy["_id"] = str(remedy["_id"])
        remedies.append(remedy)
    
    return {"remedies": remedies}

@app.get("/api/parent/quiz-history")
async def get_parent_quiz_history(current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "parent":
        raise HTTPException(status_code=403, detail="Access denied")

    raw_apaar_id = current_user.get("apaar_id", "") or ""
    # Normalize: strip spaces + uppercase (matches how quiz records store apaar_id)
    normalized_parent_apaar = "".join(raw_apaar_id.split()).upper()

    # ── Strategy 1: find the linked student by matching APAAR ID ──────────────
    child_email = None
    student_apaar_ids = set()

    if normalized_parent_apaar:
        # Try normalized form first
        student = await users_collection.find_one({
            "apaar_id": normalized_parent_apaar, "role": "student"
        })
        if not student:
            # Try with original spacing (e.g. "1234 5678 9876")
            student = await users_collection.find_one({
                "apaar_id": raw_apaar_id, "role": "student"
            })
        if student:
            child_email = student.get("email")
            # Collect all APAAR ID variants stored for this student
            student_apaar_ids.add(student.get("apaar_id", ""))
            student_apaar_ids.add("".join(str(student.get("apaar_id", "")).split()).upper())
            student_apaar_ids.discard("")

    # ── Collect quiz_history by all known identifiers ─────────────────────────
    queries = []

    # By child_email (older records)
    if child_email:
        queries.append({"child_email": child_email, "kind": "quiz_attempt"})

    # By apaar_id variants stored directly in quiz_history (newer records)
    for aid in student_apaar_ids:
        queries.append({"apaar_id": aid, "kind": "quiz_attempt"})

    # Also query by the parent's own apaar_id (case where parent & student share ID)
    if normalized_parent_apaar:
        queries.append({"apaar_id": normalized_parent_apaar, "kind": "quiz_attempt"})
    if raw_apaar_id and raw_apaar_id != normalized_parent_apaar:
        queries.append({"apaar_id": raw_apaar_id, "kind": "quiz_attempt"})

    if not queries:
        return {"quiz_history": []}

    # De-duplicate by _id
    seen_ids: set = set()
    history = []

    for query in queries:
        async for item in quiz_history_collection.find(query).sort("created_at", -1):
            item_id = str(item["_id"])
            if item_id not in seen_ids:
                seen_ids.add(item_id)
                item["_id"] = item_id
                history.append(item)

    # Sort combined results newest-first
    history.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)

    return {"quiz_history": history}


@app.get("/api/teacher/student-quiz-history")
async def get_student_quiz_history_for_teacher(
    apaar_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Teachers can view any student's quiz history by APAAR ID."""
    if current_user["role"] != "teacher":
        raise HTTPException(status_code=403, detail="Access denied")

    # Build all APAAR ID variants to maximise match coverage
    normalized = "".join(apaar_id.split()).upper()
    variants = list({normalized, apaar_id})   # de-dup in case they're identical

    # Find the student record for display name & email
    student = await users_collection.find_one({"apaar_id": normalized, "role": "student"})
    if not student:
        student = await users_collection.find_one({"apaar_id": apaar_id, "role": "student"})

    student_email = student.get("email") if student else None
    student_name = student.get("full_name", "Student") if student else "Unknown"

    # If we found a student, also collect their stored apaar_id variant
    if student and student.get("apaar_id"):
        variants.append(student["apaar_id"])
        variants = list(set(variants))

    # Build query list covering all variants and email
    queries = [{"apaar_id": v, "kind": "quiz_attempt"} for v in variants]
    if student_email:
        queries.append({"child_email": student_email, "kind": "quiz_attempt"})

    # Fetch and de-duplicate results
    seen_ids: set = set()
    history = []

    for query in queries:
        async for item in quiz_history_collection.find(query).sort("created_at", -1):
            item_id = str(item["_id"])
            if item_id not in seen_ids:
                seen_ids.add(item_id)
                item["_id"] = item_id
                history.append(item)

    history.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)

    return {
        "quiz_history": history,
        "student_name": student_name,
        "apaar_id": normalized,
    }

@app.get("/")
async def root():
    return {"message": "Student Development Tracking API", "version": "1.0.0"}

@app.get("/health")
async def health_check():
    """Health check endpoint to verify API and database connectivity"""
    try:
        # Test MongoDB connection
        await users_collection.find_one({})
        return {
            "status": "healthy",
            "database": "connected",
            "message": "API is running"
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "database": "disconnected",
            "error": str(e)
        }
