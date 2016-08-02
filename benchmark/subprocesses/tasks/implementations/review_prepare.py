import logging
from shutil import copy

import yaml
from os import makedirs, listdir
from os.path import join, exists, pardir, basename
from typing import Dict, Iterable
from typing import List

from benchmark.data.misuse import Misuse
from benchmark.data.project import Project
from benchmark.data.project_version import ProjectVersion
from benchmark.subprocesses.requirements import JavaRequirement, UrlLibRequirement
from benchmark.subprocesses.tasks.base.project_task import Response, Requirement
from benchmark.subprocesses.tasks.base.project_version_misuse_task import ProjectVersionMisuseTask
from benchmark.subprocesses.tasks.base.project_version_task import ProjectVersionTask
from benchmark.subprocesses.tasks.implementations.detect import Run
from benchmark.subprocesses.tasks.implementations.review import main_index, review_page
from benchmark.utils.io import safe_open, remove_tree, safe_write, read_yaml
from benchmark.utils.shell import Shell


class Review:
    def __init__(self, detector: str):
        self.detector = detector
        self.project_reviews = []  # type: List[ProjectReview]
        self.__current_project_review = None  # type: ProjectReview

    def start_project_review(self, project_id: str):
        self.__current_project_review = ProjectReview(project_id)
        self.project_reviews.append(self.__current_project_review)

    def start_run_review(self, name: str, run: Run):
        self.__current_project_review.start_run_review(name, run)

    def append_finding_review(self, name: str, result: str, reviewers: List[str]):
        self.__current_project_review.append_finding_review(name, result, reviewers)

    def to_html(self):
        review = "<h1>Detector: {}</h1>".format(self.detector)
        for project_review in self.project_reviews:
            review += project_review.to_html()
        return review


class ProjectReview:
    def __init__(self, project_id):
        self.project_id = project_id
        self.run_reviews = []  # type: List[RunReview]
        self.__current_run_review = None  # type: RunReview

    def start_run_review(self, name: str, run: Run):
        self.run_reviews.append(RunReview(name, run))

    def append_finding_review(self, name: str, result: str, reviewers: List[str]):
        self.run_reviews[len(self.run_reviews) - 1].append_finding_review(name, result, reviewers)

    def to_html(self):
        review = """
            <h2>Project: {}</h2>
            <table>
            """.format(self.project_id)
        for version_review in self.run_reviews:
            review += version_review.to_html()
        review += """
            </table>
            """
        return review


class RunReview:
    def __init__(self, name: str, run: Run):
        self.version_id = name
        self.run = run
        self.finding_reviews = []

    def append_finding_review(self, name: str, result: str, reviewers: List[str]):
        self.finding_reviews.append(FindingReview(name, result, reviewers))

    def to_html(self):
        review = """
            <tr>
                <td>Version:</td>
                <td>{} (result: {}, findings: {}, duration: {}s)</td>
            </tr>
            <tr>
                <td></td>
                <td>
                    <table>
            """.format(self.version_id, self.run.result, len(self.run.findings), self.run.runtime)
        for misuse_review in self.finding_reviews:
            review += misuse_review.to_html()
        review += """
                    </table>
                </td>
            </tr>
            """
        return review


class FindingReview:
    def __init__(self, name: str, result: str, reviewers: List[str]):
        self.name = name
        self.result = result
        self.reviewers = reviewers

    def to_html(self):
        reviewed_by = "reviewed by " + ", ".join(self.reviewers) if self.reviewers else "none"
        return """
            <tr>
                <td>Misuse:</td>
                <td>{}</td>
                <td>[{}]</td>
                <td>{}</td>
            </tr>
            """.format(self.name, self.result, reviewed_by)


class ReviewPrepare(ProjectVersionMisuseTask):
    no_hit = 0
    potential_hit = 1

    def __init__(self, detector: str, findings_path: str, review_path: str, checkout_base_dir: str, compiles_path: str,
                 force_prepare: bool):
        super().__init__()
        self.compiles_path = compiles_path
        self.findings_path = findings_path
        self.review_path = review_path
        self.checkout_base_dir = checkout_base_dir
        self.force_prepare = force_prepare
        self.detector = detector

        self.__review = Review(self.detector)

    def get_requirements(self):
        return [JavaRequirement(), UrlLibRequirement()]

    def start(self):
        logger = logging.getLogger("review_prepare")
        logger.info("Preparing review for results of %s...", self.detector)

    def process_project(self, project: Project):
        self.__review.start_project_review(project.id)
        super().process_project(project)

    def process_project_version(self, project: Project, version: ProjectVersion):
        findings_path = join(self.findings_path, project.id, version.version_id)
        self.__review.start_run_review(version.version_id, Run(findings_path))
        super().process_project_version(project, version)

    def process_project_version_misuse(self, project: Project, version: ProjectVersion, misuse: Misuse) -> Response:
        logger = logging.getLogger("review_prepare.misuse")

        findings_path = join(self.findings_path, project.id, version.version_id)
        detector_run = Run(findings_path)

        if not detector_run.is_success():
            logger.info("Skipping %s in %s: no result.", misuse, version)
            self.__append_misuse_to_review(misuse, "run: {}".format(detector_run.result), [])
            return Response.skip

        review_dir = join(project.id, version.version_id, misuse.id)
        review_site = join(review_dir, "review.html")
        review_path = join(self.review_path, review_dir)
        if exists(review_path) and not self.force_prepare:
            if exists(join(self.review_path, review_site)):
                existing_reviews = self.__get_existing_reviews(review_path)
                self.__append_misuse_review(misuse, review_site, existing_reviews)
            else:
                self.__append_misuse_no_hits(misuse)

            logger.info("%s in %s is already prepared.", misuse, version)
            return Response.ok

        logger.debug("Checking hit for %s in %s...", misuse, version)

        findings = detector_run.findings
        potential_hits = ReviewPrepare.find_potential_hits(findings, misuse)
        logger.info("Found %s potential hits for %s.", len(potential_hits), misuse)
        remove_tree(review_path)
        logger.debug("Generating review files for %s in %s...", misuse, version)

        if potential_hits:
            review_page.generate(review_path, self.detector, self.compiles_path, project, version, misuse,
                                 potential_hits)
            self.__generate_potential_hits_yaml(potential_hits, review_path)
            self.__append_misuse_review(misuse, review_site, [])
        else:
            makedirs(review_path)
            self.__append_misuse_no_hits(misuse)

        return Response.ok

    def __append_misuse_review(self, misuse: Misuse, review_site: str, existing_reviews: List[Dict[str, str]]):
        self.__append_misuse_to_review(misuse, "<a href=\"{}\">review</a>".format(review_site), existing_reviews)

    def __append_misuse_no_hits(self, misuse: Misuse):
        self.__append_misuse_to_review(misuse, "no potential hits", [])

    def __append_misuse_to_review(self, misuse: Misuse, result: str, existing_reviews: List[Dict[str, str]]):
        reviewers = [review['reviewer'] for review in existing_reviews if review.get('reviewer', None)]
        self.__review.append_finding_review(str(misuse), result, reviewers)

    @staticmethod
    def __get_existing_reviews(review_path: str) -> List[Dict[str, str]]:
        existing_review_files = [join(review_path, file) for file in listdir(review_path) if
                                 file.startswith('review') and file.endswith('.yml')]
        existing_reviews = []
        for existing_review_file in existing_review_files:
            existing_reviews.append(read_yaml(existing_review_file))
        return existing_reviews

    @staticmethod
    def __generate_potential_hits_yaml(potential_hits: List[Dict[str, str]], review_path: str):
        with safe_open(join(review_path, 'potentialhits.yml'), 'w+') as file:
            yaml.dump_all(potential_hits, file)

    def end(self):
        safe_write(self.__review.to_html(), join(self.review_path, "index.html"), append=False)

        main_review_dir = join(self.review_path, pardir)
        main_findings_dir = join(self.findings_path, pardir)
        if exists(main_findings_dir):
            main_index.generate(main_review_dir, main_findings_dir)

    @staticmethod
    def find_potential_hits(findings: Iterable[Dict[str, str]], misuse: Misuse) -> List[Dict[str, str]]:
        candidates = ReviewPrepare.__filter_by_file(findings, misuse.location.file)
        candidates = ReviewPrepare.__filter_by_method(candidates, misuse.location.method)
        return candidates

    @staticmethod
    def __filter_by_file(findings, misuse_file):
        matches = []
        for finding in findings:
            if ReviewPrepare.__matches_file(finding["file"], misuse_file):
                matches.append(finding)
        return matches

    @staticmethod
    def __matches_file(finding_file, misuse_file):
        # If file is an inner class "Outer$Inner.class", the source file is "Outer.java".
        if "$" in finding_file:
            finding_file = finding_file.split("$", 1)[0] + ".java"
        # If file is a class file "A.class", the source file is "A.java".
        if finding_file.endswith(".class"):
            finding_file = finding_file[:-5] + "java"
        return finding_file.endswith(misuse_file)

    @staticmethod
    def __filter_by_method(findings, misuse_method):
        matches = []

        for finding in findings:
            if "method" in finding:
                method = finding["method"]
                # if detector reports only method names, this ensures we don't match prefixes of method names
                if "(" not in method:
                    method += "("
                if method in misuse_method:
                    matches.append(finding)
            else:
                # don't filter if the detector reports no method
                matches.append(finding)

        if not matches:
            # fall back to match without the signature
            for finding in findings:
                method = finding["method"].split("(")[0] + "("
                if method in misuse_method:
                    matches.append(finding)

        return matches


class ReviewPrepareAll(ProjectVersionTask):
    def __init__(self, detector: str, findings_path: str, review_path: str, checkouts_path: str, compiles_path: str,
                 force_prepare: bool):
        super().__init__()
        self.compiles_path = compiles_path
        self.findings_path = findings_path
        self.review_path = review_path
        self.checkouts_path = checkouts_path
        self.force_prepare = force_prepare
        self.detector = detector

        self.__review = Review(self.detector)

    def get_requirements(self):
        return [JavaRequirement(), UrlLibRequirement()]

    def start(self):
        logger = logging.getLogger("review_prepare")
        logger.info("Preparing review of all findings of %s...", self.detector)

    def process_project(self, project: Project):
        self.__review.start_project_review(project.id)
        super().process_project(project)

    def process_project_version(self, project: Project, version: ProjectVersion):
        findings_path = join(self.findings_path, project.id, version.version_id)
        detector_run = Run(findings_path)
        self.__review.start_run_review(version.version_id, detector_run)

        if self.detector.startswith("jadet") or self.detector.startswith("tikanga"):
            url = join(project.id, version.version_id, "violations.xml")
            copy(join(self.findings_path, url), join(self.review_path, url))
            self.__review.append_finding_review("all findings",
                                                "<a href=\"{}\">download violations.xml</a>".format(url), [])
        else:
            for finding in detector_run.findings:
                url = join(project.id, version.version_id, "finding-{}.html".format(finding["id"]))
                review_page.generate2(join(self.review_path, url), self.detector, self.compiles_path, version, finding)
                self.__review.append_finding_review("Finding {}".format(finding["id"]),
                                                    "<a href=\"{}\">review</a>".format(url), [])

    def end(self):
        safe_write(self.__review.to_html(), join(self.review_path, "index.html"), append=False)
