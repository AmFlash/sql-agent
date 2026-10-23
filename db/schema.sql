CREATE TABLE Departments (
  DepartmentID INTEGER PRIMARY KEY,
  Name TEXT NOT NULL,
  Location TEXT
);
CREATE TABLE Employees (
  EmployeeID INTEGER PRIMARY KEY,
  FirstName TEXT NOT NULL,
  LastName TEXT NOT NULL,
  Email TEXT,
  HireDate DATE NOT NULL,
  Salary REAL,
  DepartmentID INTEGER REFERENCES Departments(DepartmentID),
  ManagerID INTEGER REFERENCES Employees(EmployeeID)
);
CREATE TABLE Customers (
  CustomerID INTEGER PRIMARY KEY,
  Name TEXT NOT NULL,
  Email TEXT,
  City TEXT,
  State TEXT
);
CREATE TABLE Products (
  ProductID INTEGER PRIMARY KEY,
  Name TEXT NOT NULL,
  Category TEXT,
  Price REAL NOT NULL
);
CREATE TABLE Orders (
  OrderID INTEGER PRIMARY KEY,
  CustomerID INTEGER REFERENCES Customers(CustomerID),
  EmployeeID INTEGER REFERENCES Employees(EmployeeID),
  OrderDate DATE NOT NULL,
  Status TEXT
);
CREATE TABLE OrderItems (
  OrderItemID INTEGER PRIMARY KEY,
  OrderID INTEGER REFERENCES Orders(OrderID),
  ProductID INTEGER REFERENCES Products(ProductID),
  Quantity INTEGER NOT NULL,
  UnitPrice REAL NOT NULL
);
