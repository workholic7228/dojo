import collections
import datetime

from flask import Blueprint, render_template, request, abort
from sqlalchemy import and_, cast
from CTFd.models import db, Challenges, Solves, Users
from CTFd.utils import get_config
from CTFd.utils.user import get_current_user, is_admin
from CTFd.utils.decorators import authed_only, admins_only, ratelimit

from ..models import DiscordUsers, DojoChallenges, DojoUsers, DojoStudents, DojoModules, DojoStudents
from ..utils import module_visible, module_challenges_visible, DOJOS_DIR, is_dojo_admin
from ..utils.dojo import dojo_route
from ..utils.discord import add_role, get_discord_user
from .writeups import WriteupComments, writeup_weeks, all_writeups


course = Blueprint("course", __name__)


def get_letter_grade(dojo, grade):
    for letter_grade, min_score in dojo.course["letter_grades"].items():
        if grade >= min_score:
            return letter_grade
    return "?"


def grade(dojo, users_query, *, ignore_pending=False):
    if isinstance(users_query, Users):
        users_query = Users.query.filter_by(id=users_query.id)

    now = datetime.datetime.now(datetime.timezone.utc)
    assessments = dojo.course["assessments"]

    assessment_dates = collections.defaultdict(lambda: collections.defaultdict(dict))
    for assessment in assessments:
        if assessment["type"] not in ["checkpoint", "due"]:
            continue
        assessment_dates[assessment["id"]][assessment["type"]] = (
            datetime.datetime.fromisoformat(assessment["date"]).astimezone(datetime.timezone.utc),
            assessment.get("extensions", {}),
        )

    def dated_count(label, date_type):
        if date_type is None:
            query = lambda module_id: True
        else:
            def query(module_id):
                if date_type not in assessment_dates[module_id]:
                    return None
                date, extensions = assessment_dates[module_id][date_type]
                user_date = db.case(
                    [(Solves.user_id == int(user_id), date + datetime.timedelta(days=days))
                     for user_id, days in extensions.items()],
                    else_=date
                ) if extensions else date
                return Solves.date < user_date
        return db.func.sum(
            db.case([(DojoModules.id == module_id, cast(query(module_id), db.Integer))
                     for module_id in assessment_dates],
                    else_=None)
        ).label(label)

    solves = (
        dojo
        .solves(ignore_visibility=True)
        .join(DojoModules, and_(
            DojoModules.dojo_id == DojoChallenges.dojo_id,
            DojoModules.module_index == DojoChallenges.module_index,
        ))
        .group_by(Solves.user_id, DojoModules.id)
        .order_by(Solves.user_id, DojoModules.module_index)
        .with_entities(
            Solves.user_id,
            DojoModules.id.label("module_id"),
            dated_count("checkpoint_solves", "checkpoint"),
            dated_count("due_solves", "due"),
            dated_count("all_solves", None)
        )
    ).subquery()
    user_solves = (
        users_query
        .join(solves, Users.id == solves.c.user_id, isouter=True)
        .with_entities(Users.id, *(column for column in solves.c if column.name != "user_id"))
    )

    module_names = {module.id: module.name for module in dojo.modules}
    challenge_counts = {module.id: len(module.challenges) for module in dojo.modules}

    module_solves = {}
    assigmments = {}

    def result(user_id):
        grades = []

        for assessment in assessments:
            type = assessment.get("type")

            date = datetime.datetime.fromisoformat(assessment["date"]) if type in ["checkpoint", "due"] else None
            if ignore_pending and date and date > now:
                continue

            if type == "checkpoint":
                module_id = assessment["id"]
                weight = assessment["weight"]
                percent_required = assessment.get("percent_required", 0.334)
                extension = assessment.get("extensions", {}).get(str(user_id), 0)

                module_name = module_names.get(module_id)
                if not module_name:
                    continue

                challenge_count = challenge_counts[module_id]
                checkpoint_solves, due_solves, all_solves = module_solves.get(module_id, (0, 0, 0))
                challenge_count_required = int(challenge_count * percent_required)
                user_date = date + datetime.timedelta(days=extension)

                grades.append(dict(
                    name=f"{module_name} Checkpoint",
                    date=str(user_date) + (" *" if extension else ""),
                    weight=weight,
                    progress=f"{checkpoint_solves} / {challenge_count_required}",
                    credit=bool(checkpoint_solves // (challenge_count_required)),
                ))

            if type == "due":
                module_id = assessment["id"]
                weight = assessment["weight"]
                percent_required = assessment.get("percent_required", 1.0)
                late_penalty = assessment.get("late_penalty", 0.0)
                extension = assessment.get("extensions", {}).get(str(user_id), 0)
                override = assessment.get("overrides", {}).get(str(user_id), None)

                module_name = module_names.get(module_id)
                if not module_name:
                    continue

                challenge_count = challenge_counts[module_id]
                checkpoint_solves, due_solves, all_solves = module_solves.get(module_id, (0, 0, 0))
                late_solves = all_solves - due_solves
                challenge_count_required = int(challenge_count * percent_required)
                user_date = date + datetime.timedelta(days=extension)
                late_value = 1 - late_penalty

                if not late_solves:
                    progress = f"{due_solves} / {challenge_count_required}"
                else:
                    progress = f"{due_solves} (+{late_solves}) / {challenge_count_required}"

                if override is None:
                    credit = min((due_solves + late_value * late_solves) / challenge_count_required, 1.0)
                else:
                    credit = override
                    progress = f"{progress} *"

                grades.append(dict(
                    name=f"{module_name}",
                    date=str(user_date) + (" *" if extension else ""),
                    weight=weight,
                    progress=progress,
                    credit=credit,
                ))

            if type == "manual":
                grades.append(dict(
                    name=assessment["name"],
                    weight=assessment["weight"],
                    progress=assessment.get("progress", {}).get(str(user_id), ""),
                    credit=assessment.get("credit", {}).get(str(user_id), 0.0),
                ))

            if type == "extra":
                grades.append(dict(
                    name=assessment["name"],
                    progress=assessment.get("progress", {}).get(str(user_id), ""),
                    credit=assessment.get("credit", {}).get(str(user_id), 0.0),
                ))

        overall_grade = (
            sum(grade["credit"] * grade["weight"] for grade in grades if "weight" in grade) /
            sum(grade["weight"] for grade in grades if "weight" in grade)
        )
        extra_credit = (
            sum(grade["credit"] for grade in grades if "weight" not in grade)
        )
        overall_grade += extra_credit
        overall_grade = round(overall_grade, 4)
        letter_grade = get_letter_grade(dojo, overall_grade)

        return dict(user_id=user_id,
                    grades=grades,
                    overall_grade=overall_grade,
                    letter_grade=letter_grade)

    user_id = None
    previous_user_id = None
    for user_id, module_id, checkpoint_solves, due_solves, all_solves in user_solves:
        if user_id != previous_user_id:
            if previous_user_id is not None:
                yield result(previous_user_id)
                module_solves = {}
            previous_user_id = user_id
        if module_id is not None:
            module_solves[module_id] = (
                int(checkpoint_solves) if checkpoint_solves is not None else 0,
                int(due_solves) if due_solves is not None else 0,
                int(all_solves) if all_solves is not None else 0,
            )
    if user_id:
        yield result(user_id)


@course.route("/dojo/<dojo>/course")
@course.route("/dojo/<dojo>/course/<resource>")
@dojo_route
def view_course(dojo, resource=None):
    if not dojo.course:
        abort(404)

    if request.args.get("user"):
        if not dojo.is_admin():
            abort(403)
        user = Users.query.filter_by(id=request.args.get("user")).first_or_404()
        name = f"{user.name}'s"
    else:
        user = get_current_user()
        name = "Your"

    ignore_pending = request.args.get("ignore_pending") is not None

    grades = {}
    identity = {}

    setup = {
        step: "incomplete"
        for step in ["create_account", "link_student", "create_discord", "link_discord", "join_discord"]
    }

    if user:
        grades = next(grade(dojo, user, ignore_pending=ignore_pending))

        student = DojoStudents.query.filter_by(dojo=dojo, user=user).first()
        identity["identity_name"] = dojo.course.get("student_id", "Identity")
        identity["identity_value"] = student.token if student else None

        setup["create_account"] = "complete"

        if student and student.token in dojo.course.get("students", []):
            setup["link_student"] = "complete"
        elif student:
            setup["link_student"] = "unknown"

        if DiscordUsers.query.filter_by(user=user).first():
            setup["create_discord"] = "complete"
            setup["link_discord"] = "complete"

        if get_discord_user(user.id):
            setup["join_discord"] = "complete"
        else:
            setup["join_discord"] = "incomplete"

    return render_template("course.html", name=name, **grades, **identity, **setup, user=user, dojo=dojo)


@course.route("/dojo/<dojo>/course/identity", methods=["PATCH"])
@dojo_route
@authed_only
@ratelimit(method="PATCH", limit=10, interval=60)
def update_identity(dojo):
    if not dojo.course:
        abort(404)

    user = get_current_user()
    dojo_user = DojoUsers.query.filter_by(dojo=dojo, user=user).first()

    if dojo_user and dojo_user.type == "admin":
        return {"success": False, "error": "Cannot identify admin"}

    identity = request.json.get("identity", "").strip()
    if not dojo_user:
        dojo_user = DojoStudents(dojo=dojo, user=user, token=identity)
        db.session.add(dojo_user)
    else:
        dojo_user.type = "student"
        dojo_user.token = identity
    db.session.commit()

    students = set(dojo.course.get("students", []))
    if students and identity not in students:
        return {"success": True, "warning": f"Your identity ({identity}) is not on the official student roster"}

    discord_role = dojo.course.get("discord_role")
    if discord_role:
        discord_user = get_discord_user(user.id)
        if discord_user is False:
            return {"success": True, "warning": "Your Discord account is not linked"}
        if discord_user is None:
            return {"success": True, "warning": "Your Discord account has not joined the official Discord server"}
        add_role(discord_user["user"]["id"], discord_role)

    return {"success": True}


@course.route("/dojo/<dojo>/admin/grades")
@dojo_route
@authed_only
def view_all_grades(dojo):
    if not dojo.course:
        abort(404)

    if not dojo.is_admin():
        abort(403)

    ignore_pending = request.args.get("ignore_pending") is not None

    users = (
        Users
        .query
        .join(DojoStudents, DojoStudents.user_id == Users.id)
        .filter(DojoStudents.dojo == dojo,
                DojoStudents.token.in_(dojo.course.get("students", [])))
    )
    grades = sorted(grade(dojo, users, ignore_pending=ignore_pending),
                    key=lambda grade: grade["overall_grade"],
                    reverse=True)

    average_grade = sum(grade["overall_grade"] for grade in grades) / len(grades) if grades else 0.0
    average_letter_grade = get_letter_grade(dojo, average_grade)
    average_grade_summary = f"{average_letter_grade} ({average_grade * 100:.2f}%)"
    average_grade_details = []
    cumulative_count = 0
    for letter_grade in dojo.course["letter_grades"]:
        count = sum(1 for grade in grades if grade["letter_grade"] == letter_grade)
        cumulative_count += count
        percent = f"{count / len(grades) * 100:.2f}%" if grades else "0.00%"
        cumulative_percent = f"{cumulative_count / len(grades) * 100:.2f}%" if grades else "0.00%"
        average_grade_details.append({
            "Grade": letter_grade,
            "Count": count,
            "Percent": percent,
            "Cumulative Percent": cumulative_percent,
        })
    grade_statistics = {
        "Average": (average_grade_summary, average_grade_details),
    }

    students = {student.user_id: student.token for student in dojo.students}

    return render_template("grades_admin.html",
                           grades=grades,
                           grade_statistics=grade_statistics,
                           students=students)

@course.route("/dojo/<dojo>/admin/users/<user_id>")
@dojo_route
@authed_only
def view_user_info(dojo, user_id):
    if not dojo.course:
        abort(404)

    if not dojo.is_admin():
        abort(403)

    user = Users.query.filter_by(id=user_id).first_or_404()
    student = DojoStudents.query.filter_by(dojo=dojo, user=user).first()
    identity = {}
    identity["identity_name"] = dojo.course.get("student_id", "Identity")
    identity["identity_value"] = student.token if student else None

    discord_user = get_discord_user(user.id)

    return render_template("dojo_admin_user.html",
                           dojo=dojo,
                           user=user,
                           discord_user=discord_user,
                           **identity)
