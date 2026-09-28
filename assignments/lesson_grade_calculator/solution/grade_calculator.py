def get_student_name():
    name = input("Enter student name: ")
    return name


def get_grade(prompt):
    grade = int(input(prompt))
    return grade


def is_valid_grade(grade):
    if grade < 0 or grade > 100:
        return False
    else:
        return True


def all_grades_valid(grade1, grade2, grade3):
    if is_valid_grade(grade1) and is_valid_grade(grade2) and is_valid_grade(grade3):
        return True
    else:
        return False


def calculate_average(grade1, grade2, grade3):
    average = (grade1 + grade2 + grade3) / 3
    return average


def get_letter_grade(average):
    if average > 89:
        return "A"
    elif average > 79:
        return "B"
    elif average > 69:
        return "C"
    elif average > 59:
        return "D"
    else:
        return "F"


def get_pass_status(letter_grade):
    if letter_grade == "F":
        return "Fail"
    else:
        return "Pass"


def get_result_message(status):
    if status == "Pass":
        return "You passed. You can go to Disney World!"
    else:
        return "You failed. You must go to summer school."


def get_summer_school_classes(average, status):
    if status == "Pass":
        return 0
    elif average > 49:
        return 1
    elif average > 39:
        return 2
    elif average > 29:
        return 3
    else:
        return 4


def print_report(name, grade1, grade2, grade3, average, letter_grade, status, result_message, summer_classes):
    print()
    print("===== Final Grade Report =====")
    print(f"Student: {name}")
    print(f"Grade 1: {grade1}")
    print(f"Grade 2: {grade2}")
    print(f"Grade 3: {grade3}")
    print(f"Average: {average}")
    print(f"Letter Grade: {letter_grade}")
    print(f"Status: {status}")
    print(f"Result: {result_message}")
    print(f"Summer School Classes Required: {summer_classes}")
    print("==============================")


def main():
    name = get_student_name()

    grade1 = get_grade("Enter grade 1: ")
    grade2 = get_grade("Enter grade 2: ")
    grade3 = get_grade("Enter grade 3: ")

    if all_grades_valid(grade1, grade2, grade3):
        average = calculate_average(grade1, grade2, grade3)
        letter_grade = get_letter_grade(average)
        status = get_pass_status(letter_grade)
        result_message = get_result_message(status)
        summer_classes = get_summer_school_classes(average, status)

        print_report(
            name,
            grade1,
            grade2,
            grade3,
            average,
            letter_grade,
            status,
            result_message,
            summer_classes,
        )
    else:
        print("Invalid grade. Grades must be from 0 to 100.")


if __name__ == "__main__":
    main()
