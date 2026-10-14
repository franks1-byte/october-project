-- Sample database: students, courses, and enrollments.
-- Load it in the shell with:   .read examples/school.sql

CREATE TABLE students (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    year INTEGER,
    city TEXT DEFAULT 'Cagayan de Oro'
);

CREATE TABLE courses (
    code TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    units INTEGER
);

CREATE TABLE enrollments (
    id INTEGER PRIMARY KEY,
    student_id INTEGER NOT NULL,
    course_code TEXT NOT NULL,
    grade REAL
);

CREATE INDEX enroll_student ON enrollments(student_id);
CREATE INDEX enroll_course ON enrollments(course_code);

BEGIN;
INSERT INTO students (name, year, city) VALUES
    ('Andrea', 2, 'Cebu'), ('Brix', 2, NULL), ('Carlo', 1, 'Davao'), ('Dianne', 3, 'Iligan'),
    ('Enzo', 2, 'Cebu'), ('Faith', 1, 'Bukidnon'), ('Gab', 3, 'Davao'), ('Hannah', 2, 'Iligan');
INSERT INTO students (name, year) VALUES ('Ivan', 1), ('Jessa', 3);

INSERT INTO courses VALUES
    ('IT211', 'Object-Oriented Programming', 3), ('IT212', 'Data Structures', 3),
    ('STS101', 'Science, Technology and Society', 3), ('MATH21', 'Discrete Math', 3), ('PE3', 'Team Sports', 2);

INSERT INTO enrollments (student_id, course_code, grade) VALUES
    (1, 'IT211', 1.25), (1, 'IT212', 1.5), (1, 'STS101', 1.75),
    (2, 'IT211', 2.0), (2, 'MATH21', 2.5),
    (3, 'IT211', 1.0), (3, 'STS101', 1.25), (3, 'PE3', 1.0),
    (4, 'IT212', 2.75), (4, 'MATH21', 3.0),
    (5, 'IT211', 1.75), (5, 'IT212', NULL),
    (6, 'STS101', 1.5), (6, 'PE3', 1.25),
    (7, 'IT212', 1.25), (7, 'MATH21', 1.5), (7, 'IT211', 1.5),
    (8, 'IT211', 2.25), (8, 'STS101', 2.0),
    (9, 'PE3', 1.75);
COMMIT;

-- Try these:
-- SELECT s.name, AVG(e.grade) AS gwa FROM students s JOIN enrollments e ON e.student_id = s.id
--   GROUP BY s.id ORDER BY gwa LIMIT 3;
-- SELECT c.title, COUNT(e.id) AS enrolled FROM courses c LEFT JOIN enrollments e ON e.course_code = c.code
--   GROUP BY c.code ORDER BY enrolled DESC;
-- EXPLAIN SELECT * FROM enrollments WHERE student_id = 3;
