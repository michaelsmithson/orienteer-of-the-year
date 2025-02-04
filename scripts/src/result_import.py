import logging
import re
from csv import DictReader
from datetime import date, datetime, timedelta
from os import path
from sys import argv, stdin
from typing import Optional
from invalid import prompt

from fuzzywuzzy import fuzz, process
from helpers.connection import commit_and_close, session, tables
from sqlalchemy import update
from helpers.args import get_filename, get_season

logging.basicConfig(level=logging.WARNING, format="")
season = get_season()
filename = get_filename()

LIKELY_MATCH = 0.80
POSSIBLE_MATCH = 0.75


class _Match(object):

    def __init__(self, name: str, score: int) -> None:
        self.name = name
        self.score = score

    def is_certain(self) -> bool:
        return self.score >= 100

    def is_likely(self) -> bool:
        return self.score >= (LIKELY_MATCH * 100)

    def is_possible(self) -> bool:
        return self.score >= (POSSIBLE_MATCH * 100)


class _Person(object):
    def __init__(
        self,
        first_name: str,
        last_name: str,
        gender: str,
    ) -> None:
        self.first_name = first_name
        self.last_name = last_name
        self.gender = gender

    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    def __str__(self) -> str:
        return self.full_name()


class _Competitor(_Person):
    def __init__(
            self,
            first_name: str,
            last_name: str,
            dob: Optional[date],
            gender: str,
            grade: str,
            time: Optional[timedelta],
            status: str,
            raw_points: int = None,
            points: int = None,
    ) -> None:
        super().__init__(first_name, last_name, gender)
        self.dob = dob if dob else None
        self.grade = grade
        self.time = time
        self.status = status
        self.raw_points = raw_points
        self.points = points


class _Member(_Person):
    def __init__(
            self,
            first_name: str,
            last_name: str,
            dob: date,
            gender: str,
            memberid: int,
    ) -> None:
        super().__init__(first_name, last_name, gender)
        self.dob = dob
        self.memberid = memberid


def _get_member_from_match(
        members_list: list[_Member],
        match: _Match,
) -> _Member:
    for member in members_list:
        if _normalize_name(member.full_name()) == match.name:
            return member
    raise ValueError


def _commit_member(member: _Member, competitor: _Competitor) -> None:
    session.merge(
        tables.Result(
            member_id=member.memberid,
            year=season,
            event_number=event_number,
            race_number=race.number,
            race_grade=competitor.grade,
            time=competitor.time,
            status=competitor.status,
            raw_points=competitor.raw_points,
            points=competitor.points
        )
    )


def _find_possible_match(matches: list[_Match]) -> None:
    for match in matches:
        member = _get_member_from_match(members, match)
        gender_matches = member.gender == competitor.gender
        if competitor.dob:
            dob_matches = member.dob.year == competitor.dob.year
            # now can check for details and match more confidently
            if dob_matches and gender_matches:
                if match.is_likely():
                    # all details match and names are close so add
                    logging.info(
                        "ADD:  perfect match found for "
                        f"competitor {competitor}",
                    )
                    _commit_member(member, competitor)
                elif match.is_possible():
                    # all details match but names are not so close
                    # add with warning
                    logging.warning(
                        f"ADD:  match between {competitor} and {member} "
                        "is not certain, but DOB and gender "
                        f"match ({match.score})",
                    )
                    _commit_member(member, competitor)
                break
            else:
                # details do not match
                if match.is_likely():
                    logging.warning(
                        f"OMIT: details between {competitor} and {member} "
                        "do not match",
                    )
        else:
            # no dob, can match less confidently
            if match.is_likely() and gender_matches:
                # names are close but no details to back up
                # add with warning
                logging.warning(
                    f"ADD:  match between {competitor} and {member} "
                    f"is not certain, but gender matches ({match.score})",
                )
                _commit_member(member, competitor)
                break
            if match.is_possible() and gender_matches:
                # names are less close and no details to back up
                # omit with warning
                logging.warning(
                    f"OMIT: match between {competitor} and {member} "
                    f"is close ({match.score})",
                )


def _certain_match(top_match: _Match) -> None:
    member = _get_member_from_match(members, top_match)

    gender_matches = member.gender == competitor.gender

    dob_matches = True
    if competitor.dob:
        assert member.dob is not None
        dob_matches = member.dob.year == competitor.dob.year

    if dob_matches and gender_matches:
        logging.info(f"ADD:  competitor {competitor}")
        _commit_member(member, competitor)
    else:
        logging.warning(
            f"ADD:  details of {competitor} "
            "do not match with the members database "
            f"(DoB:{dob_matches} S:{gender_matches})",
        )
        _commit_member(member, competitor)


def _get_matches(competitor: _Competitor, members: list[_Member]) -> None:
    matches_temp = process.extract(
        _normalize_name(competitor.full_name()),
        [_normalize_name(member.full_name()) for member in members],
        limit=3,
        scorer=fuzz.token_sort_ratio,
    )
    matches = []
    for match in matches_temp:
        matches.append(_Match(name=match[0], score=match[1]))
    top_match = matches[0]

    if len(list(filter(lambda match: match.is_certain(), matches))) > 1:
        logging.warning(
            f"OMIT:multiple members with name {competitor.full_name()} found",
        )
    elif top_match.is_certain():
        _certain_match(top_match)
    elif top_match.is_possible():
        _find_possible_match(matches)
    else:
        non_members.append(competitor)
        logging.info(
            "OMIT: no membership found for competitor"
            f"{competitor.full_name()}",
        )


event_number = prompt.List(
    "event", {f"OY{event.number}": event.number for event in session.query(
        tables.Event).filter_by(year=season).all()}).prompt()


races = session.query(
    tables.Race).filter_by(
        year=season,
    event_number=event_number)

race = prompt.List(
    "race", {
        f"{race.number}: {race.map}": race for race in races}).prompt()


members = []
competitors = []
non_members: list[_Competitor] = []

for member in session.query(tables.Member).filter_by(year=season):
    members.append(
        _Member(
            first_name=member.first_name,
            last_name=member.last_name,
            dob=member.DOB,
            gender=member.gender,
            memberid=member.member_id,
        ),
    )

csv_timeformats = ["%H:%M:%S", "%M:%S"]

status_codes = {
    0: "OK",
    1: "DNS",
    2: "DNF",
    3: "MP",
    4: "DQ",
    5: "NT"}

grades = session.query(tables.Grade)

with open(filename, "r") as csvfile:
    reader = DictReader(csvfile)

    race_grades = {*[(row["Short"], row["Long"]) for row in reader]}
    if {*[race_grade[0] for race_grade in race_grades]} == {*[
            grade.grade_id for grade in grades]}:
        for race_grade in race_grades:
            session.merge(
                tables.RaceGrade(
                    year=season,
                    event_number=event_number,
                    race_number=race.number,
                    grade_id=race_grade[0],
                    race_grade=race_grade[0]
                )
            )
    else:
        for grade in grades:
            selected_grade = prompt.List(
                f"match grade for {grade.name}", {
                    grade_name: grade_id for grade_id, grade_name in race_grades}).prompt()
            session.merge(
                tables.RaceGrade(
                    year=season,
                    event_number=event_number,
                    race_number=race.number,
                    grade_id=grade.grade_id,
                    race_grade=selected_grade))

with open(filename, "r") as csvfile:
    reader = DictReader(csvfile)
    for competitor_raw in reader:
        deltatime = None
        if competitor_raw["Start"] and competitor_raw["Finish"]:
            finish_time = None
            start_time = None
            for timeformat in csv_timeformats:
                try:
                    finish_time = datetime.strptime(
                        competitor_raw["Finish"], timeformat)
                except ValueError:
                    pass
                try:
                    start_time = datetime.strptime(
                        competitor_raw["Start"], timeformat)
                except ValueError:
                    pass
            if finish_time and start_time:
                deltatime = finish_time - start_time
                if deltatime < timedelta(0, 0, 0):
                    for timeformat in csv_timeformats:
                        try:
                            t = datetime.strptime(
                                competitor_raw["Time"], timeformat)
                        except ValueError:
                            pass
                    deltatime = timedelta(
                        hours=t.hour, minutes=t.minute, seconds=t.second
                    )
        else:
            if status_codes[int(competitor_raw["Classifier"])] in ["OK", "MP"]:
                raise Exception("No time but OK classifier")

        competitors.append(
            _Competitor(
                first_name=competitor_raw["First name"],
                last_name=competitor_raw["Surname"],
                dob=date(
                    int(competitor_raw["YB"]),
                    1,
                    1,
                ) if competitor_raw["YB"] else None,
                gender=competitor_raw["S"],
                grade=competitor_raw["Short"],
                time=getattr(deltatime, "seconds") if deltatime else None,
                status=status_codes[int(
                    competitor_raw["Classifier"])
                ],
                raw_points=competitor_raw["Points"] or None if race.discipline_id == "SCO" else None,
                points=competitor_raw["Score Result"] or None if race.discipline_id == "SCO" else None,
            ),
        )


def _normalize_name(name: str) -> str:
    return re.sub(r"\s|-|_", "", name.lower())


for competitor in competitors:
    _get_matches(competitor, members)


logging.info("competitors matched in this import:")
logging.info(", ".join(
    [non_member.full_name() for non_member in non_members],
))

logging.critical("continue with import?")
answer = stdin.readline().strip()
if answer.lower() in {"y", "yes"}:
    session.execute(
        update(
            tables.Season).where(
            tables.Season.year == season).values(
                last_event=event_number))
    commit_and_close()
    logging.critical("import complete")
else:
    logging.critical("import aborted")
